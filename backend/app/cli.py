from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Sequence, TextIO

from fastapi import UploadFile

from .domain import (
    ConflictResolution,
    HumanDecisionResponse,
    RunRecord,
    RunState,
)


InputFunction = Callable[[str], str]


def _batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be an integer") from exc
    if not 1 <= parsed <= 500:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 500")
    return parsed


class Console:
    def __init__(
        self,
        *,
        output: TextIO = sys.stdout,
        error: TextIO = sys.stderr,
        input_function: InputFunction = input,
    ) -> None:
        self.output = output
        self.error = error
        self.input_function = input_function

    def line(self, message: str = "") -> None:
        print(message, file=self.output, flush=True)

    def warning(self, message: str) -> None:
        print(f"[warning] {message}", file=self.error, flush=True)

    def ask(self, prompt: str) -> str:
        try:
            return self.input_function(prompt).strip()
        except EOFError as exc:
            raise RuntimeError(
                "Interactive input is unavailable. Resume this run in a terminal."
            ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="excel-merge-agent",
        description="Plan, review, execute, and resume template-guided Excel merges.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    merge = subparsers.add_parser("merge", help="Start a new interactive merge run.")
    merge.add_argument("--template", type=Path, required=True, help="Template .xlsx workbook.")
    merge.add_argument(
        "--source",
        dest="sources",
        type=Path,
        nargs="+",
        action="extend",
        default=[],
        help="One or more source .xlsx workbooks; the option may be repeated.",
    )
    merge.add_argument(
        "--source-dir",
        type=Path,
        action="append",
        default=[],
        help="Add all .xlsx files in a directory.",
    )
    merge.add_argument(
        "--recursive",
        action="store_true",
        help="Search source directories recursively.",
    )
    merge.add_argument(
        "--batch-size",
        type=_batch_size,
        default=50,
        metavar="N",
        help="Source workbooks per executor batch (1-500; default: 50).",
    )
    merge.add_argument("--model-profile", help="Configured model profile id.")
    merge.add_argument(
        "--output",
        type=Path,
        default=Path("merged.xlsx"),
        help="Approved destination for the verified workbook.",
    )
    merge.add_argument(
        "--audit-output",
        type=Path,
        help="Approved destination for the audit JSON (default: beside --output).",
    )

    resume = subparsers.add_parser("resume", help="Resume an interactive persisted run.")
    resume.add_argument("run_id")
    resume.add_argument("--output", type=Path, help="Workbook destination before approval.")
    resume.add_argument("--audit-output", type=Path, help="Audit destination before approval.")

    status = subparsers.add_parser("status", help="Show one persisted run.")
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true", dest="as_json")

    runs = subparsers.add_parser("runs", help="List recent persisted runs.")
    runs.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _components():
    # Import lazily so parser/help use does not initialize persistence or model config.
    from .main import model_connections, model_factory, run_service

    return run_service, model_factory, model_connections


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _validate_workbook(path: Path, role: str) -> Path:
    resolved = _normalized_path(path)
    if not resolved.is_file():
        raise ValueError(f"{role} workbook does not exist: {resolved}")
    if resolved.suffix.lower() != ".xlsx":
        raise ValueError(f"{role} workbook must use .xlsx: {resolved}")
    return resolved


def _collect_sources(args: argparse.Namespace, template: Path) -> list[Path]:
    candidates = list(args.sources)
    pattern = "**/*.xlsx" if args.recursive else "*.xlsx"
    for directory in args.source_dir:
        resolved_directory = _normalized_path(directory)
        if not resolved_directory.is_dir():
            raise ValueError(f"Source directory does not exist: {resolved_directory}")
        candidates.extend(sorted(resolved_directory.glob(pattern)))

    sources: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        source = _validate_workbook(candidate, "Source")
        if source == template or source in seen:
            continue
        seen.add(source)
        sources.append(source)
    if not sources:
        raise ValueError("Provide at least one source with --source or --source-dir")
    return sources


def _default_audit_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.audit.json")


def _print_plan(run: RunRecord, console: Console) -> None:
    if run.spec is None:
        return
    console.line("\n[agent] Proposed merge plan")
    console.line(f"  Run: {run.id}")
    console.line(f"  Model: {run.planner.model if run.planner else 'unknown'}")
    console.line(f"  Plan hash: {run.spec_hash}")
    for index, operation in enumerate(run.spec.operations, start=1):
        console.line(
            f"  {index}. [{operation.mode}] {operation.input_sheet} -> "
            f"{operation.sheet}: {operation.description}"
        )
    if run.spec.rationale:
        console.line(f"  Rationale: {run.spec.rationale}")
    shifted = []
    if run.compiled_plan:
        for operation in run.compiled_plan.operations:
            for source in operation.sources:
                count = sum(
                    column.source_column != column.target_column for column in source.columns
                )
                if count:
                    shifted.append(f"{source.source_file}: {count} shifted column(s)")
    if shifted:
        console.line("  Semantic alignment: " + "; ".join(shifted))
    console.line(f"  Preflight questions: {sum(not item.resolved for item in run.conflicts)}")


def _select_number(console: Console, prompt: str, count: int) -> int:
    while True:
        answer = console.ask(prompt)
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        console.warning(f"Enter a number from 1 to {count}.")


def _resolve_conflict(service, run: RunRecord, console: Console) -> RunRecord:
    conflict = next(item for item in run.conflicts if not item.resolved)
    console.line("\n[input required] The workbook evidence cannot determine this choice.")
    console.line(f"  {conflict.message}")
    console.line(f"  Source: {conflict.source_file} | Sheet: {conflict.sheet}")
    if conflict.cell:
        console.line(f"  Cell: {conflict.cell} | Value: {conflict.actual!r}")
    for index, action in enumerate(conflict.allowed_actions, start=1):
        console.line(f"  {index}. {action.replace('_', ' ')}")
    selected = conflict.allowed_actions[
        _select_number(console, "Select an action: ", len(conflict.allowed_actions))
    ]

    identical = [
        item
        for item in run.conflicts
        if not item.resolved and item.type == conflict.type and item.actual == conflict.actual
    ]
    scope = "this_cell"
    if len(identical) > 1:
        answer = console.ask(
            f"Apply this action to all {len(identical)} identical questions? [y/N]: "
        ).lower()
        if answer in {"y", "yes"}:
            scope = "identical_in_run"
    return service.resolve_conflict(
        run.id,
        conflict.id,
        ConflictResolution(action=selected, scope=scope),
    )


def _resolve_runtime_question(service, run: RunRecord, console: Console) -> RunRecord:
    decision = next(item for item in run.decisions if not item.resolved)
    console.line("\n[input required] Execution needs information unavailable in the files.")
    console.line(f"  {decision.question}")
    console.line(f"  {decision.message}")
    for index, option in enumerate(decision.options, start=1):
        console.line(f"  {index}. {option.label} — {option.description}")
    selected = decision.options[
        _select_number(console, "Select an action: ", len(decision.options))
    ]
    return service.resolve_decision(
        run.id,
        decision.id,
        HumanDecisionResponse(action=selected.action),
    )


def _approved_cli_destinations(run: RunRecord) -> tuple[Path | None, Path | None]:
    if run.write_approval is None or len(run.write_approval.output_paths) < 3:
        return None, None
    workbook = Path(run.write_approval.output_paths[2])
    audit = (
        Path(run.write_approval.output_paths[3])
        if len(run.write_approval.output_paths) >= 4
        else None
    )
    return workbook, audit


def _confirm_write(
    run: RunRecord,
    output: Path,
    audit_output: Path,
    console: Console,
) -> bool:
    console.line("\n[write approval required]")
    console.line("The reviewed plan is ready to run the deterministic Python executor.")
    console.line(f"  Workbook: {output}")
    console.line(f"  Audit: {audit_output}")
    existing = [str(path) for path in (output, audit_output) if path.exists()]
    if existing:
        console.line("  Existing files that will be replaced: " + ", ".join(existing))
    answer = console.ask("Type 'approve' to write these files, or press Enter to pause: ")
    return answer.lower() == "approve"


async def _plan(service, factory, run: RunRecord, console: Console) -> RunRecord:
    console.line("[tool] inspect_workbooks — extracting formulas, labels, and structure")
    run = service.prepare_inspection(run.id)
    if run.state == RunState.FAILED:
        raise RuntimeError(run.error or "Workbook inspection failed")
    console.line(f"[model] planning with profile {run.model_profile_id or 'default'}")
    evidence = service.planning_evidence(run.id)
    runtime = factory.create(run.model_profile_id)
    try:
        spec, provenance = await runtime.plan_merge(run.template.stored_path, evidence)
    except Exception as exc:
        service.fail_planning(run.id, exc)
        raise
    finally:
        await runtime.close()
    run = service.accept_plan(run.id, spec, provenance)
    _print_plan(run, console)
    return run


async def _continue_run(
    service,
    factory,
    run_id: str,
    output: Path | None,
    audit_output: Path | None,
    console: Console,
) -> int:
    for _ in range(1000):
        run = service.get(run_id)
        if run.state in {RunState.FILES_UPLOADED, RunState.INSPECTING}:
            run = await _plan(service, factory, run, console)
            continue
        if run.state == RunState.AWAITING_USER_INPUT:
            if any(not item.resolved for item in run.conflicts):
                _resolve_conflict(service, run, console)
                continue
            if any(not item.resolved for item in run.decisions):
                _resolve_runtime_question(service, run, console)
                continue
            raise RuntimeError("Run is waiting for input but has no pending question")
        if run.state == RunState.AWAITING_WRITE_APPROVAL:
            output = _normalized_path(output or Path("merged.xlsx"))
            audit_output = _normalized_path(audit_output or _default_audit_path(output))
            if not _confirm_write(run, output, audit_output, console):
                console.line(f"[paused] Run {run.id} was saved without writing files.")
                console.line(f"Resume with: excel-merge-agent resume {run.id}")
                return 2
            service.approve(
                run.id,
                run.spec_hash or "",
                additional_output_paths=[output, audit_output],
            )
            console.line("[approved] Exact plan and destinations authorized for one write.")
            continue
        if run.state == RunState.PLAN_READY:
            approved_output, approved_audit = _approved_cli_destinations(run)
            if approved_output is not None:
                if output is not None and _normalized_path(output) != approved_output.resolve():
                    raise ValueError("--output differs from the destination already approved")
                output, audit_output = approved_output, approved_audit
            console.line("[tool] execute_merge_configuration — Python executor started")
            await asyncio.to_thread(service.execute, run.id)
            continue
        if run.state == RunState.COMPLETED:
            approved_output, approved_audit = _approved_cli_destinations(run)
            if approved_output is not None:
                service.publish_outputs(run.id, approved_output, approved_audit)
                output, audit_output = approved_output, approved_audit
            console.line("\n[completed] Merged workbook verified.")
            checks = len(run.verification.checks) if run.verification else 0
            console.line(f"  Verification checks: {checks}")
            console.line(f"  Workbook: {output or run.output_path}")
            console.line(f"  Audit: {audit_output or run.audit_path}")
            console.line(f"  Run id: {run.id}")
            return 0
        if run.state == RunState.CREATED:
            raise ValueError("This run has no uploaded workbooks")
        if run.state == RunState.CANCELLED:
            console.line(f"[cancelled] Run {run.id} did not write an output workbook.")
            return 2
        if run.state == RunState.FAILED:
            raise RuntimeError(run.error or "Merge run failed")
        raise RuntimeError(
            f"Run state {run.state.value!r} cannot be resumed safely from this process"
        )
    raise RuntimeError("Run exceeded the interactive transition limit")


async def _start_merge(args: argparse.Namespace, console: Console) -> int:
    service, factory, connections = _components()
    template = _validate_workbook(args.template, "Template")
    sources = _collect_sources(args, template)
    registry = connections.registry()
    profile_id = args.model_profile or registry.default_profile
    registry.profile(profile_id)

    output = _normalized_path(args.output)
    audit_output = _normalized_path(args.audit_output or _default_audit_path(output))
    run = service.create(profile_id)
    try:
        service.configure_batch(run.id, args.batch_size)
        console.line(f"[agent] Run {run.id} created")
        console.line(
            f"[tool] stage_workbooks — 1 template, {len(sources)} sources, "
            f"batch size {args.batch_size}"
        )
        with ExitStack() as stack:
            template_file = stack.enter_context(template.open("rb"))
            source_files = [stack.enter_context(path.open("rb")) for path in sources]
            run = await service.save_files(
                run.id,
                UploadFile(file=template_file, filename=template.name),
                [
                    UploadFile(file=file, filename=path.name)
                    for file, path in zip(source_files, sources, strict=True)
                ],
            )
        return await _continue_run(
            service, factory, run.id, output, audit_output, console
        )
    except BaseException:
        console.warning(f"Run id for resumption or diagnosis: {run.id}")
        raise


async def _resume(args: argparse.Namespace, console: Console) -> int:
    service, factory, _ = _components()
    run = service.get(args.run_id)
    approved_output, approved_audit = _approved_cli_destinations(run)
    output = _normalized_path(args.output) if args.output else approved_output
    audit_output = _normalized_path(args.audit_output) if args.audit_output else approved_audit
    if approved_output is not None and output is not None and output != approved_output.resolve():
        raise ValueError("--output differs from the destination already approved")
    if approved_audit is not None and audit_output is not None and audit_output != approved_audit.resolve():
        raise ValueError("--audit-output differs from the destination already approved")
    console.line(f"[agent] Resuming run {run.id} from state {run.state.value}")
    return await _continue_run(
        service, factory, run.id, output, audit_output, console
    )


def _show_status(run: RunRecord, console: Console) -> None:
    console.line(f"Run: {run.id}")
    console.line(f"State: {run.state.value}")
    console.line(f"Model profile: {run.model_profile_id or 'default'}")
    console.line(f"Sources: {len(run.sources)} | Batch size: {run.batch_size}")
    console.line(f"Plan hash: {run.spec_hash or '-'}")
    console.line(f"Pending conflicts: {sum(not item.resolved for item in run.conflicts)}")
    console.line(f"Pending runtime questions: {sum(not item.resolved for item in run.decisions)}")
    if run.error:
        console.line(f"Error: {run.error}")
    if run.events:
        console.line("Events:")
        for event in run.events:
            console.line(f"  {event['at']} [{event['kind']}] {event['message']}")


async def dispatch(args: argparse.Namespace, console: Console) -> int:
    if args.command == "merge":
        return await _start_merge(args, console)
    if args.command == "resume":
        return await _resume(args, console)
    service, _, _ = _components()
    if args.command == "status":
        run = service.get(args.run_id)
        if args.as_json:
            console.line(run.model_dump_json(indent=2))
        else:
            _show_status(run, console)
        return 0
    if args.command == "runs":
        runs = service.list()
        if args.as_json:
            console.line(json.dumps([run.model_dump(mode="json") for run in runs], indent=2))
        else:
            for run in runs:
                console.line(
                    f"{run.id}  {run.state.value:24}  {run.updated_at}  "
                    f"sources={len(run.sources)}"
                )
        return 0
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    try:
        return asyncio.run(dispatch(args, console))
    except KeyboardInterrupt:
        console.warning("Interrupted. The persisted run can be resumed by run id.")
        return 130
    except (KeyError, ValueError, RuntimeError) as exc:
        console.warning(str(exc))
        return 1
    except Exception as exc:
        console.warning(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
