from __future__ import annotations

import contextvars
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ai

from .domain import MergeSpec, PlannerProvenance
from .generic_workbook import validate_merge_spec


@dataclass
class PlanningContext:
    template_path: Path
    proposals: list[MergeSpec]


_planning_context: contextvars.ContextVar[PlanningContext | None] = contextvars.ContextVar(
    "planning_context",
    default=None,
)


@ai.tool
async def submit_merge_plan(
    configuration: MergeSpec,
) -> dict[str, Any]:
    """Submit the complete executor-ready Excel merge plan after interpreting the template guidelines.

    Every operation must include concrete row/range parameters. Cite relevant template cells as
    `Sheet!A1` strings. This tool validates the proposal against the actual template before accepting it.
    """
    context = _planning_context.get()
    if context is None:
        raise RuntimeError("Planning context is not active")
    spec = configuration
    validate_merge_spec(spec, context.template_path)
    context.proposals.append(spec)
    return {
        "accepted": True,
        "operation_count": len(spec.operations),
        "plan_hash": spec.digest(),
    }


planning_agent = ai.Agent(tools=[submit_merge_plan])


async def create_model_plan(
    model: ai.Model,
    model_id: str,
    template_path: Path,
    evidence: dict[str, Any],
    *,
    extra_body: dict[str, Any] | None = None,
    profile_id: str | None = None,
    provider: str | None = None,
) -> tuple[MergeSpec, PlannerProvenance]:
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    evidence_sha256 = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    base_messages = [
        ai.system_message(
            "You are an Excel merge planning agent. Interpret template instructions, examples, labels, "
            "merged headers, and source structures. You never edit workbooks and never calculate final "
            "values. Your only task is to call submit_merge_plan. Its configuration argument must be a "
            "typed object with schema_version=2, template_family, operations, stack_groups, rationale, and "
            "guideline_citations. Each operation object uses these fields: id, sheet, optional source_sheet, "
            "mode (add or concatenate), description, alignment (row_key or position), key_column, row_keys, "
            "value_columns, column_alignment (auto, header_path, or position), data_start_row, end_marker_prefix, column_count, row_filter, style_template_row, "
            "placement (in_place or stack), stack_group, and stack_order. row_filter contains four arrays: "
            "exclude_prefixes, exclude_exact_values, exclude_contains, and exclude_regexes. Derive every filter "
            "value from the extracted template and source evidence; never assume a language-specific marker. "
            "Each stack group uses id, sheet, start_row, column_count, end_marker_prefix, and retain_end_marker. "
            "Every operation is automatically applied to every uploaded source workbook, so never create one "
            "operation per source file. One concatenate operation copies the qualifying rows from all sources "
            "in upload order. If template instructions distinguish independent detail rows from shared total, "
            "summary, or aggregate rows, create a stacked add operation for every repeated aggregate label and "
            "exclude those labels through row_filter. Every nonblank label already present in a template body "
            "must be classified either by row_filter or by a stacked add operation; validation enforces this. "
            "Use the labeled_rows numeric_columns evidence "
            "to identify which columns of repeated labeled rows are addable. "
            "Create as many add and concatenate operations as the workbook requires. Add operations must "
            "specify target sheet, source_sheet when different, row_key or position alignment, key/range, "
            "one-based value columns, and in_place or stack placement. Concatenate operations must specify "
            "source and target sheet, source row boundaries, width, exclusions, style row, and a stack group. "
            "Use column_alignment=auto unless workbook instructions explicitly require positional columns. "
            "Auto maps source fields to template fields by hierarchical merged-header paths and tolerates inserted or reordered source columns. "
            "Use a stack group when several operation results share a rebuilt output body; stack_order controls "
            "their order, and the group controls the target start row and retained note/marker. Never include a "
            "stacked add row in concatenate output. guideline_citations must contain only "
            "exact existing cell references in Sheet!A1 form, without descriptions. "
            "Keep formula_policy=freeze_displayed_value and retain notes. "
            "Do not invent sheets, rows, or columns not supported by the evidence."
        ),
        ai.user_message(
            "Analyze this extracted workbook evidence and submit the merge plan through the tool:\n"
            + evidence_json
        ),
    ]
    proposals: list[MergeSpec] = []
    validation_errors: list[str] = []
    params = ai.InferenceRequestParams(
        tool_calling=ai.ToolCallingParams(
            tool_choice=ai.ToolRef("submit_merge_plan"),
            parallel_tool_calls=False,
        ),
        extra_body=extra_body,
    )
    max_attempts = 3
    attempts_used = 0
    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        messages = list(base_messages)
        if validation_errors:
            messages.append(
                ai.user_message(
                    "Your previous submit_merge_plan call was rejected. Retry the complete plan now. "
                    "Never call the tool with an empty object. The outer tool argument must be exactly "
                    "{\"configuration\": { ...complete MergeSpec... }}. Correct this error:\n"
                    + validation_errors[-1][:4000]
                )
            )
        token = _planning_context.set(
            PlanningContext(template_path=template_path, proposals=proposals)
        )
        try:
            async with planning_agent.run(model, messages, params=params) as stream:
                failed_tool_calls = 0
                async for event in stream:
                    if proposals:
                        break
                    if isinstance(event, ai.events.ToolCallResult):
                        failed_tool_calls += 1
                        if event.exception is not None:
                            validation_errors.append(str(event.exception))
                        if failed_tool_calls >= 3:
                            break
        except Exception as exc:
            validation_errors.append(str(exc))
        finally:
            _planning_context.reset(token)
        if proposals:
            break
    if not proposals:
        detail = validation_errors[-1] if validation_errors else "no tool submission was received"
        raise ValueError(f"The model did not submit a valid merge plan: {detail}")
    return proposals[0], PlannerProvenance(
        kind="model",
        model=model_id,
        evidence_sha256=evidence_sha256,
        attempts=attempts_used,
        profile_id=profile_id,
        provider=provider,
    )
