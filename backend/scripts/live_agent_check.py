from __future__ import annotations

import asyncio
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.datastructures import UploadFile

from app import agent
from app.domain import ConflictResolution
from app.model_config import ModelConnectionRepository
from app.model_runtime import ModelRuntime
from app.persistence import RunRepository
from app.service import RunService
from tests.synthetic_workbooks import build_synthetic_workbooks


ROOT = Path(__file__).resolve().parents[2]


def upload(path: Path) -> UploadFile:
    return UploadFile(file=BytesIO(path.read_bytes()), filename=path.name)


async def main() -> None:
    check_root = ROOT / "backend" / "var" / "agent-tool-check"
    service = RunService(RunRepository(check_root / "runs.sqlite3"), check_root / "runs")
    agent.configure_agent(service)
    runtime = ModelRuntime(ModelConnectionRepository())
    try:
        with TemporaryDirectory(prefix="merge-agent-live-fixtures-") as temporary:
            fixtures = build_synthetic_workbooks(Path(temporary))
            run = service.create(runtime.profile_id)
            run = await service.save_files(
                run.id,
                upload(fixtures / "template.xlsx"),
                [upload(fixtures / "source_1.xlsx"), upload(fixtures / "source_2.xlsx")],
            )
            service.prepare_inspection(run.id)
            evidence = service.planning_evidence(run.id)
            run = service.get(run.id)
            if run.template is None:
                raise RuntimeError("Template disappeared during live check")
            spec, provenance = await runtime.plan_merge(run.template.stored_path, evidence)
            run = service.accept_plan(run.id, spec, provenance)
            for conflict in [item for item in run.conflicts if not item.resolved]:
                if conflict.recommended_action != "treat_as_zero":
                    raise RuntimeError(f"Unexpected live-check conflict: {conflict.model_dump_json()}")
                run = service.resolve_conflict(
                    run.id,
                    conflict.id,
                    ConflictResolution(action=conflict.recommended_action, scope="this_cell"),
                )
            run = service.approve(run.id, run.spec_hash or "")
            run = await asyncio.to_thread(service.execute, run.id)
            if run.state.value != "completed" or not run.verification or not run.verification.passed:
                raise RuntimeError(f"Live agent run failed verification: {run.model_dump_json()}")
            print(
                json.dumps(
                    {
                        "run_id": run.id,
                        "state": run.state.value,
                        "model": provenance.model,
                        "operations": [item.model_dump() for item in spec.operations],
                        "stack_groups": [item.model_dump() for item in spec.stack_groups],
                        "output_path": run.output_path,
                        "verification": run.verification.model_dump() if run.verification else None,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
