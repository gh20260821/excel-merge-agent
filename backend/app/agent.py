from __future__ import annotations

import asyncio
from typing import Any

import ai

from .service import RunService


_service: RunService | None = None


def configure_agent(service: RunService) -> None:
    global _service
    _service = service


def _runs() -> RunService:
    if _service is None:
        raise RuntimeError("merge agent is not configured")
    return _service


@ai.tool
async def get_run_summary(run_id: str) -> dict[str, Any]:
    """Get the current merge plan, state, conflicts, and verification summary."""
    run = _runs().get(run_id)
    return {
        "id": run.id,
        "state": run.state.value,
        "plan": {
            "operations": [
                {
                    "id": operation.id,
                    "mode": operation.mode,
                    "source_sheet": operation.input_sheet,
                    "target_sheet": operation.sheet,
                    "description": operation.description,
                }
                for operation in run.spec.operations
            ],
            "rationale": run.spec.rationale,
            "guideline_citations": run.spec.guideline_citations,
        }
        if run.spec
        else None,
        "compiled_mapping": [
            {
                "operation_id": operation.operation_id,
                "sources": [
                    {
                        "source_file": source.source_file,
                        "mapped_rows": len(source.rows),
                        "mapped_columns": len(source.columns),
                        "shifted_columns": [
                            {
                                "source": column.source_column,
                                "target": column.target_column,
                                "header_path": column.header_path,
                            }
                            for column in source.columns
                            if column.source_column != column.target_column
                        ],
                    }
                    for source in operation.sources
                ],
            }
            for operation in (run.compiled_plan.operations if run.compiled_plan else [])
        ],
        "spec_hash": run.spec_hash,
        "write_approved": bool(
            run.write_approval and run.write_approval.spec_hash == run.spec_hash
        ),
        "conflicts": [
            {
                "id": conflict.id,
                "message": conflict.message,
                "resolved": conflict.resolved,
                "resolution": conflict.resolution,
            }
            for conflict in run.conflicts
        ],
        "runtime_questions": [
            {
                "id": decision.id,
                "phase": decision.phase,
                "code": decision.code,
                "question": decision.question,
                "message": decision.message,
                "context": decision.context,
                "options": [option.model_dump() for option in decision.options],
                "resolved": decision.resolved,
                "selected_action": decision.selected_action,
            }
            for decision in run.decisions
        ],
        "excluded_sources": run.excluded_sources,
        "batch_size": run.batch_size,
        "batch_progress": run.batch_progress.model_dump() if run.batch_progress else None,
        "execution_attempts": run.execution_attempts,
        "recovery_attempts": [item.model_dump() for item in run.recovery_attempts],
        "verification": run.verification.model_dump() if run.verification else None,
    }


@ai.tool(require_approval=True)
async def execute_approved_merge(
    run_id: str,
    spec_hash: str,
) -> dict[str, Any]:
    """Approve, write, and verify the exact server-stored compiled merge plan."""
    run = _runs().get(run_id)
    if run.spec_hash != spec_hash:
        raise ValueError("The supplied plan is not the current reviewed plan")
    _runs().approve(run_id, spec_hash)
    completed = await asyncio.to_thread(_runs().execute, run_id)
    return {
        "run_id": completed.id,
        "state": completed.state.value,
        "verification": completed.verification.model_dump()
        if completed.verification
        else None,
        "pending_decision": next(
            (
                item.model_dump()
                for item in completed.decisions
                if not item.resolved
            ),
            None,
        ),
    }


chat_agent = ai.Agent(tools=[get_run_summary, execute_approved_merge])
