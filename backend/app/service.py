from __future__ import annotations

import re
import shutil
import uuid
from math import ceil
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile

from .domain import (
    BatchProgress,
    ConflictResolution,
    DecisionOption,
    HumanDecisionRequest,
    HumanDecisionResponse,
    MergeSpec,
    PlannerProvenance,
    RecoveryAttempt,
    RunRecord,
    RunState,
    UploadedWorkbook,
    WriteApprovalGrant,
    utc_now,
)
from .persistence import RunRepository
from .generic_workbook import (
    build_planning_evidence,
    compile_merge_plan,
    detect_conflicts,
    execute_merge,
    inspect_workbook,
    sha256_file,
    validate_merge_spec,
    RecoverableExecutionIssue,
)


DECISION_OPTIONS: dict[str, tuple[str, str]] = {
    "retry_execution": (
        "Retry execution",
        "Rerun from the untouched template using the same approved plan.",
    ),
    "exclude_source_and_retry": (
        "Exclude source and retry",
        "Exclude the affected source workbook, then rerun from the template.",
    ),
    "return_to_planning": (
        "Return to planning",
        "Invalidate approval and ask the planning agent for a revised plan.",
    ),
    "abort": ("Abort run", "Cancel this merge without producing an output workbook."),
}

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000


def _safe_filename(name: str) -> str:
    filename = Path(name).name
    filename = re.sub(r"[^\w\-.（）()\u4e00-\u9fff]+", "_", filename)
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("Only .xlsx files are supported")
    return filename


async def _store_xlsx_upload(upload: UploadFile, path: Path) -> None:
    size = 0
    try:
        with path.open("wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError("Workbook exceeds the 50 MB upload limit")
                destination.write(chunk)
        try:
            with ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ZIP_ENTRIES:
                    raise ValueError("Workbook contains too many packaged files")
                if any(entry.flag_bits & 1 for entry in entries):
                    raise ValueError("Encrypted workbooks are not supported")
                uncompressed = sum(entry.file_size for entry in entries)
                compressed = sum(max(1, entry.compress_size) for entry in entries)
                if uncompressed > MAX_UNCOMPRESSED_BYTES or uncompressed / compressed > 200:
                    raise ValueError("Workbook package expands beyond safe processing limits")
                names = {entry.filename for entry in entries}
                if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                    raise ValueError("The upload is not a valid .xlsx workbook package")
        except BadZipFile as exc:
            raise ValueError("The upload is not a readable .xlsx workbook") from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise


class RunService:
    def __init__(self, repository: RunRepository, run_root: Path) -> None:
        self.repository = repository
        self.run_root = run_root
        run_root.mkdir(parents=True, exist_ok=True)

    def create(self, model_profile_id: str | None = None) -> RunRecord:
        run = RunRecord(id=str(uuid.uuid4()), model_profile_id=model_profile_id)
        run.add_event(
            "run_created", "Merge run created.", model_profile_id=model_profile_id
        )
        return self.repository.save(run)

    def get(self, run_id: str) -> RunRecord:
        run = self.repository.get(run_id)
        changed = self._normalize_source_ids(run)
        changed = self._normalize_spec_digest(run) or changed
        changed = self._normalize_legacy_state(run) or changed
        if changed:
            self.repository.save(run)
        return run

    def list(self) -> list[RunRecord]:
        runs = self.repository.list()
        for run in runs:
            changed = self._normalize_source_ids(run)
            changed = self._normalize_spec_digest(run) or changed
            changed = self._normalize_legacy_state(run) or changed
            if changed:
                self.repository.save(run)
        return runs

    @staticmethod
    def _normalize_source_ids(run: RunRecord) -> bool:
        changed = False
        workbooks = ([run.template] if run.template else []) + list(run.sources)
        for index, workbook in enumerate(workbooks):
            if workbook and not workbook.id:
                workbook.id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{run.id}:{workbook.role}:{index}:{workbook.sha256}",
                    )
                )
                changed = True
        return changed

    @staticmethod
    def _normalize_spec_digest(run: RunRecord) -> bool:
        """Upgrade unfinished plan hashes when schema defaults gain safe semantics."""
        if run.spec is None or run.state in {
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
        }:
            return False
        current_digest = run.spec.digest()
        if run.spec_hash == current_digest:
            return False
        previous_digest = run.spec_hash
        run.spec_hash = current_digest
        run.compiled_plan = None
        run.approved_spec_hash = None
        run.write_approval = None
        unresolved = any(
            item.severity == "blocking" and not item.resolved for item in run.conflicts
        ) or any(not item.resolved for item in run.decisions)
        run.state = (
            RunState.AWAITING_USER_INPUT
            if unresolved
            else RunState.AWAITING_WRITE_APPROVAL
        )
        run.add_event(
            "plan_schema_upgraded",
            "The unfinished plan was upgraded to the current safe configuration schema.",
            previous_spec_hash=previous_digest,
            current_spec_hash=current_digest,
        )
        return True

    @staticmethod
    def _normalize_legacy_state(run: RunRecord) -> bool:
        """Map pre-single-gate records onto the current resumable state model."""
        if run.state not in {RunState.AWAITING_APPROVAL, RunState.AWAITING_HUMAN}:
            return False
        pending_questions = any(not item.resolved for item in run.decisions)
        unresolved_conflicts = any(
            item.severity == "blocking" and not item.resolved for item in run.conflicts
        )
        previous = run.state
        if pending_questions or unresolved_conflicts:
            run.state = RunState.AWAITING_USER_INPUT
        else:
            # Old plan approvals are intentionally not promoted into write grants:
            # the user must see the one current approval immediately before writing.
            run.approved_spec_hash = None
            run.write_approval = None
            run.state = RunState.AWAITING_WRITE_APPROVAL
        run.add_event(
            "legacy_state_migrated",
            "Legacy approval state migrated to the single write-approval workflow.",
            previous_state=previous.value,
            current_state=run.state.value,
        )
        return True

    def save_conversation(
        self,
        run_id: str,
        messages: list[dict[str, object]],
    ) -> RunRecord:
        """Persist an AI SDK UI-message snapshot for chat hydration after refresh."""
        run = self.get(run_id)
        run.conversation = messages[-200:]
        run.updated_at = utc_now()
        return self.repository.save(run)

    def configure_batch(self, run_id: str, batch_size: int) -> RunRecord:
        """Configure bounded source processing without changing plan semantics."""
        if not 1 <= batch_size <= 500:
            raise ValueError("Batch size must be between 1 and 500 source workbooks")
        run = self.get(run_id)
        if run.state in {
            RunState.EXECUTING,
            RunState.RECOVERING,
            RunState.COMPLETED,
            RunState.CANCELLED,
        }:
            raise ValueError(f"Batch size cannot be changed while state={run.state.value}")
        if run.batch_size == batch_size:
            return run
        run.batch_size = batch_size
        run.batch_progress = None
        if run.write_approval is not None:
            run.write_approval = None
            run.approved_spec_hash = None
            run.state = RunState.AWAITING_WRITE_APPROVAL
        run.add_event(
            "batch_configuration_updated",
            f"Source batch size set to {batch_size} workbooks.",
            batch_size=batch_size,
        )
        return self.repository.save(run)

    async def save_files(
        self,
        run_id: str,
        template: UploadFile,
        sources: list[UploadFile],
    ) -> RunRecord:
        run = self.get(run_id)
        run_dir = self.run_root / run_id / "inputs"
        run_dir.mkdir(parents=True, exist_ok=True)

        template_name = _safe_filename(template.filename or "template.xlsx")
        template_path = run_dir / "template" / template_name
        template_path.parent.mkdir(parents=True, exist_ok=True)
        await _store_xlsx_upload(template, template_path)
        run.template = UploadedWorkbook(
            id=str(uuid.uuid4()),
            role="template",
            filename=template_name,
            stored_path=str(template_path),
            sha256=sha256_file(template_path),
        )

        run.sources = []
        for index, upload in enumerate(sources, start=1):
            source_name = _safe_filename(upload.filename or f"source-{index}.xlsx")
            source_path = run_dir / f"source-{index:03d}" / source_name
            source_path.parent.mkdir(parents=True, exist_ok=True)
            await _store_xlsx_upload(upload, source_path)
            run.sources.append(
                UploadedWorkbook(
                    id=str(uuid.uuid4()),
                    role="source",
                    filename=source_name,
                    stored_path=str(source_path),
                    sha256=sha256_file(source_path),
                )
            )

        if not run.sources:
            raise ValueError("At least one source workbook is required")
        run.state = RunState.FILES_UPLOADED
        run.add_event(
            "files_uploaded",
            f"Stored one template and {len(run.sources)} source workbooks.",
        )
        return self.repository.save(run)

    def prepare_inspection(self, run_id: str) -> RunRecord:
        run = self.get(run_id)
        if run.template is None or not run.sources:
            raise ValueError("Upload a template and at least one source workbook first")
        run.state = RunState.INSPECTING
        run.add_event("inspection_started", "Inspecting workbook structures.")
        self.repository.save(run)
        try:
            paths = [Path(run.template.stored_path), *[Path(item.stored_path) for item in run.sources]]
            run.profiles = [inspect_workbook(path) for path in paths]
            run.template.sha256 = sha256_file(Path(run.template.stored_path))
            for source in run.sources:
                source.sha256 = sha256_file(Path(source.stored_path))
            run.spec = None
            run.spec_hash = None
            run.compiled_plan = None
            run.approved_spec_hash = None
            run.write_approval = None
            run.planner = None
            run.conflicts = []
            run.decisions = []
            run.excluded_sources = []
            run.batch_progress = None
            run.execution_attempts = 0
            run.recovery_attempts = []
            run.output_path = None
            run.audit_path = None
            run.verification = None
            run.error = None
            run.add_event(
                "inspection_completed",
                "Workbook evidence extracted; model planning is starting.",
            )
            return self.repository.save(run)
        except Exception as exc:
            return self.fail_planning(run_id, exc, "Workbook inspection failed.")

    def planning_evidence(self, run_id: str) -> dict[str, object]:
        run = self.get(run_id)
        if run.template is None or not run.sources:
            raise ValueError("Upload files before creating planning evidence")
        return build_planning_evidence(
            Path(run.template.stored_path),
            [Path(item.stored_path) for item in run.sources],
        )

    def accept_plan(
        self,
        run_id: str,
        spec: MergeSpec,
        planner: PlannerProvenance,
    ) -> RunRecord:
        run = self.get(run_id)
        if run.template is None or not run.sources:
            raise ValueError("Upload files before accepting a merge plan")
        template_path = Path(run.template.stored_path)
        validate_merge_spec(spec, template_path)
        run.spec = spec
        run.spec_hash = spec.digest()
        run.approved_spec_hash = None
        run.write_approval = None
        run.planner = planner
        run.conflicts = detect_conflicts(template_path, run.sources, spec)
        structurally_blocked = {
            item.source_id
            for item in run.conflicts
            if item.source_id
            and item.type
            in {"missing_sheet", "schema_mismatch", "duplicate_row_key", "duplicate_source"}
        }
        run.compiled_plan = compile_merge_plan(
            template_path,
            [item for item in run.sources if item.id not in structurally_blocked],
            spec,
        )
        run.decisions = []
        run.excluded_sources = []
        run.batch_progress = None
        run.execution_attempts = 0
        run.recovery_attempts = []
        run.output_path = None
        run.audit_path = None
        run.verification = None
        run.state = (
            RunState.AWAITING_USER_INPUT
            if any(not conflict.resolved for conflict in run.conflicts)
            else RunState.AWAITING_WRITE_APPROVAL
        )
        run.error = None
        run.add_event(
            "model_plan_ready",
            f"Model submitted {len(spec.operations)} operations; preflight found {len(run.conflicts)} conflicts.",
            spec_hash=run.spec_hash,
            model=planner.model,
        )
        return self.repository.save(run)

    def fail_planning(
        self,
        run_id: str,
        exc: Exception,
        message: str = "Model planning failed.",
    ) -> RunRecord:
        run = self.get(run_id)
        run.state = RunState.FAILED
        run.error = str(exc)
        run.add_event("planning_failed", message)
        self.repository.save(run)
        return run

    def inspect_and_plan(self, run_id: str, spec: MergeSpec) -> RunRecord:
        """Deterministic test helper; production planning is model-driven."""
        run = self.prepare_inspection(run_id)
        if run.state == RunState.FAILED:
            return run
        planner = PlannerProvenance(
            kind="test",
            model="deterministic-test-fixture",
            evidence_sha256="test",
        )
        try:
            return self.accept_plan(run_id, spec, planner)
        except Exception as exc:
            self.fail_planning(run_id, exc)
            raise

    def approve(
        self,
        run_id: str,
        spec_hash: str,
        additional_output_paths: list[Path] | None = None,
    ) -> RunRecord:
        """Persist the single authorization granted immediately before file writes."""
        run = self.get(run_id)
        if run.spec_hash != spec_hash:
            raise ValueError("The merge plan changed; review the current plan before approving")
        unresolved = [conflict for conflict in run.conflicts if not conflict.resolved]
        if unresolved:
            raise ValueError(
                f"Answer {len(unresolved)} question(s) that require user knowledge before approving writes"
            )
        if run.template is None:
            raise ValueError("The template is unavailable")
        if run.compiled_plan is None or run.compiled_plan.spec_hash != spec_hash:
            raise ValueError("Compile the reviewed plan against the current inputs before approval")
        output_dir = self.run_root / run_id / "outputs"
        approved_paths = [str(output_dir / "merged.xlsx"), str(output_dir / "audit.json")]
        for path in additional_output_paths or []:
            resolved = str(path.expanduser().resolve())
            if resolved not in approved_paths:
                approved_paths.append(resolved)
        run.write_approval = WriteApprovalGrant(
            spec_hash=spec_hash,
            template_sha256=run.template.sha256,
            source_sha256s={item.id: item.sha256 for item in run.sources},
            conflict_resolutions={
                item.id: item.resolution for item in run.conflicts if item.resolution
            },
            excluded_sources=list(run.excluded_sources),
            output_paths=approved_paths,
            compiled_plan_hash=run.compiled_plan.digest() if run.compiled_plan else None,
            batch_size=run.batch_size,
        )
        run.approved_spec_hash = spec_hash
        run.state = RunState.PLAN_READY
        run.add_event(
            "write_approved",
            "Writing the staged workbook and audit was approved for the exact reviewed configuration.",
            spec_hash=spec_hash,
            output_paths=run.write_approval.output_paths,
        )
        return self.repository.save(run)

    def publish_outputs(
        self,
        run_id: str,
        workbook_destination: Path,
        audit_destination: Path | None = None,
    ) -> RunRecord:
        """Atomically copy verified outputs only to paths covered by write approval."""
        run = self.get(run_id)
        if run.state != RunState.COMPLETED or not run.verification or not run.verification.passed:
            raise ValueError("Only a completed and verified run can publish outputs")
        if run.write_approval is None or run.write_approval.spec_hash != run.spec_hash:
            raise ValueError("The completed run does not have a valid write approval")
        if not run.output_path or not run.audit_path:
            raise ValueError("The verified workbook or audit report is unavailable")

        destinations = [workbook_destination]
        sources = [Path(run.output_path)]
        if audit_destination is not None:
            destinations.append(audit_destination)
            sources.append(Path(run.audit_path))

        approved = {str(Path(path).expanduser().resolve()) for path in run.write_approval.output_paths}
        resolved_destinations = [path.expanduser().resolve() for path in destinations]
        unapproved = [str(path) for path in resolved_destinations if str(path) not in approved]
        if unapproved:
            raise ValueError(
                "Output destination was not included in the write approval: "
                + ", ".join(unapproved)
            )

        for source, destination in zip(sources, resolved_destinations, strict=True):
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_name(f".{destination.name}.{run.id}.staging")
            staging.unlink(missing_ok=True)
            try:
                shutil.copy2(source, staging)
                staging.replace(destination)
            finally:
                staging.unlink(missing_ok=True)

        run.add_event(
            "cli_outputs_published",
            "Verified outputs were published to the CLI-approved destinations.",
            output_paths=[str(path) for path in resolved_destinations],
        )
        return self.repository.save(run)

    def resolve_conflict(
        self,
        run_id: str,
        conflict_id: str,
        resolution: ConflictResolution,
    ) -> RunRecord:
        run = self.get(run_id)
        target = next((item for item in run.conflicts if item.id == conflict_id), None)
        if target is None:
            raise KeyError(conflict_id)
        if resolution.action not in target.allowed_actions:
            raise ValueError(
                f"Action {resolution.action!r} is not allowed for conflict {conflict_id!r}"
            )
        targets = [target]
        if resolution.scope == "identical_in_run":
            targets = [
                item
                for item in run.conflicts
                if item.type == target.type and item.actual == target.actual
            ]
        for item in targets:
            if resolution.action not in item.allowed_actions:
                raise ValueError(
                    f"Action {resolution.action!r} is not allowed for every conflict in this scope"
                )
            item.resolution = resolution.action
            item.resolution_scope = resolution.scope
            if resolution.action == "exclude_source":
                source_identity = item.source_id or item.source_file
                if source_identity not in run.excluded_sources:
                    run.excluded_sources.append(source_identity)
        if run.write_approval is not None:
            run.write_approval = None
            run.approved_spec_hash = None
        if resolution.action == "abort":
            run.state = RunState.CANCELLED
            run.add_event("run_cancelled", "A human reviewer aborted the merge.")
            return self.repository.save(run)
        unresolved = [item for item in run.conflicts if not item.resolved]
        if not unresolved:
            run.state = (
                RunState.PLAN_READY
                if run.write_approval and run.write_approval.spec_hash == run.spec_hash
                else RunState.AWAITING_WRITE_APPROVAL
            )
        else:
            run.state = RunState.AWAITING_USER_INPUT
        run.add_event(
            "conflict_resolved",
            f"Applied {resolution.action} to {len(targets)} conflict(s).",
        )
        return self.repository.save(run)

    def execute(self, run_id: str, supplied_spec: MergeSpec | None = None) -> RunRecord:
        run = self.get(run_id)
        if run.state == RunState.CANCELLED:
            raise ValueError("This merge run was cancelled")
        if run.approved_spec_hash != run.spec_hash:
            raise ValueError("Approve the pending local file write before execution")
        if run.write_approval is None or run.write_approval.spec_hash != run.spec_hash:
            raise ValueError("No valid write approval exists for this configuration")
        if run.compiled_plan is None or run.compiled_plan.spec_hash != run.spec_hash:
            raise ValueError("The reviewed plan has not been compiled against the current inputs")
        if run.write_approval.compiled_plan_hash != run.compiled_plan.digest():
            raise ValueError("The compiled source-to-target mapping changed after approval")
        if run.write_approval.batch_size != run.batch_size:
            raise ValueError("The batch configuration changed after write approval")
        if supplied_spec is not None and supplied_spec.digest() != run.spec_hash:
            raise ValueError("Agent tool configuration does not match the approved plan")
        unresolved = [item for item in run.conflicts if item.severity == "blocking" and not item.resolved]
        if unresolved:
            raise ValueError(f"Resolve {len(unresolved)} blocking conflict(s) before execution")
        pending = [item for item in run.decisions if not item.resolved]
        if pending:
            raise ValueError("Resolve the pending runtime question before execution")
        if run.state != RunState.PLAN_READY:
            raise ValueError(f"Run is not ready for execution (state={run.state.value})")
        run.state = RunState.EXECUTING
        run.execution_attempts += 1
        active_source_count = sum(
            1
            for source in run.sources
            if source.id not in run.excluded_sources
            and source.filename not in run.excluded_sources
        )
        operation_count = len(supplied_spec.operations if supplied_spec else run.spec.operations)
        batches_per_operation = ceil(active_source_count / run.batch_size) if active_source_count else 0
        run.batch_progress = BatchProgress(
            batch_size=run.batch_size,
            total_sources=active_source_count,
            total_work_units=batches_per_operation * operation_count,
            status="running",
            started_at=utc_now(),
        )
        run.add_event(
            "executor_tool_started",
            "Approved operation configuration submitted to the Python executor.",
            spec_hash=run.spec_hash,
            operation_count=operation_count,
            attempt=run.execution_attempts,
        )
        run.add_event(
            "batch_execution_started",
            f"Processing {active_source_count} sources in batches of {run.batch_size}.",
            batch_size=run.batch_size,
            source_count=active_source_count,
            total_work_units=run.batch_progress.total_work_units,
        )
        self.repository.save(run)
        run_dir = self.run_root / run_id / "outputs"
        output_path = run_dir / "merged.xlsx"
        audit_path = run_dir / "audit.json"
        staging_output = run_dir / "merged.staging.xlsx"
        staging_audit = run_dir / "audit.staging.json"
        staging_output.unlink(missing_ok=True)
        staging_audit.unlink(missing_ok=True)
        try:
            current_template_hash = sha256_file(Path(run.template.stored_path))
            if current_template_hash != run.template.sha256:
                return self._invalidate_changed_inputs(
                    run, "template_changed", "The template changed after planning."
                )
            for source in run.sources:
                if source.id in run.excluded_sources or source.filename in run.excluded_sources:
                    continue
                if sha256_file(Path(source.stored_path)) != source.sha256:
                    return self._invalidate_changed_inputs(
                        run,
                        "source_changed",
                        f"Source workbook {source.filename!r} changed after planning.",
                    )
            verification = self._execute_with_recovery(
                run, staging_output, staging_audit, supplied_spec
            )
            if not verification.passed:
                staging_output.unlink(missing_ok=True)
                staging_audit.unlink(missing_ok=True)
                run.output_path = None
                run.audit_path = None
                run.verification = verification
                run.state = RunState.FAILED
                run.error = "The staged workbook failed verification after automatic recovery."
                run.add_event(
                    "recovery_exhausted",
                    "Automatic rebuild attempts were exhausted; no files were published.",
                    checks=verification.checks,
                )
                return self.repository.save(run)
            staging_output.replace(output_path)
            staging_audit.replace(audit_path)
            run.output_path = str(output_path)
            run.audit_path = str(audit_path)
            run.verification = verification
            run.state = RunState.COMPLETED
            if run.batch_progress:
                run.batch_progress.status = "completed"
                run.batch_progress.completed_work_units = run.batch_progress.total_work_units
                run.batch_progress.current_operation = None
                run.batch_progress.completed_at = utc_now()
            run.add_event(
                "batch_execution_completed",
                f"Finished all bounded source batches (batch size {run.batch_size}).",
                batch_size=run.batch_size,
                total_work_units=run.batch_progress.total_work_units if run.batch_progress else 0,
            )
            run.add_event(
                "execution_completed",
                "Merged workbook created and verified.",
            )
        except RecoverableExecutionIssue as issue:
            staging_output.unlink(missing_ok=True)
            staging_audit.unlink(missing_ok=True)
            run.output_path = None
            run.audit_path = None
            run.verification = None
            run.error = None
            if run.batch_progress:
                run.batch_progress.status = "failed"
            options = [
                DecisionOption(
                    action=action,
                    label=DECISION_OPTIONS[action][0],
                    description=DECISION_OPTIONS[action][1],
                )
                for action in issue.allowed_actions
            ]
            decision = HumanDecisionRequest(
                phase=issue.phase,
                code=issue.code,
                question=issue.question,
                message=issue.message,
                context=issue.context,
                options=options,
            )
            run.decisions.append(decision)
            run.state = RunState.AWAITING_USER_INPUT
            run.add_event(
                "runtime_decision_requested",
                "Execution needs business information that is not available in the workbooks.",
                decision_id=decision.id,
                code=decision.code,
            )
        except Exception as exc:
            staging_output.unlink(missing_ok=True)
            staging_audit.unlink(missing_ok=True)
            run.state = RunState.FAILED
            run.error = str(exc)
            if run.batch_progress:
                run.batch_progress.status = "failed"
            run.add_event("execution_failed", "Merge execution failed.")
            self.repository.save(run)
            raise
        return self.repository.save(run)

    def _execute_with_recovery(
        self,
        run: RunRecord,
        staging_output: Path,
        staging_audit: Path,
        supplied_spec: MergeSpec | None,
        max_attempts: int = 2,
    ):
        """Rebuild a failed staged result before involving the user."""
        verification = None
        recovery: RecoveryAttempt | None = None

        def report_progress(
            operation_id: str,
            batch_index: int,
            batches_in_operation: int,
            processed_sources: int,
        ) -> None:
            progress = run.batch_progress
            if progress is None:
                return
            progress.status = "running"
            progress.current_operation = operation_id
            progress.current_batch = batch_index
            progress.batches_in_operation = batches_in_operation
            progress.processed_sources = max(progress.processed_sources, processed_sources)
            progress.completed_work_units = min(
                progress.total_work_units,
                progress.completed_work_units + 1,
            )
            run.updated_at = utc_now()
            self.repository.save(run)

        for attempt in range(1, max_attempts + 1):
            if run.batch_progress:
                run.batch_progress.completed_work_units = 0
                run.batch_progress.current_batch = 0
                run.batch_progress.current_operation = None
                run.batch_progress.processed_sources = 0
                run.batch_progress.status = "running"
            if attempt > 1 and recovery is None:
                recovery = RecoveryAttempt(
                    code="verification_failed",
                    category="deterministic",
                    action="rebuild_from_untouched_template",
                    attempt=attempt - 1,
                    max_attempts=max_attempts - 1,
                    message="Rebuilding the staged workbook from the untouched template.",
                )
                run.recovery_attempts.append(recovery)
                run.state = RunState.RECOVERING
                run.add_event(
                    "recovery_started",
                    recovery.message,
                    recovery_id=recovery.id,
                    attempt=recovery.attempt,
                    max_attempts=recovery.max_attempts,
                )
                self.repository.save(run)
                staging_output.unlink(missing_ok=True)
                staging_audit.unlink(missing_ok=True)
            try:
                verification = execute_merge(
                    run,
                    staging_output,
                    staging_audit,
                    supplied_spec,
                    progress_callback=report_progress,
                )
            except RecoverableExecutionIssue:
                # These issues encode an unresolved business/structural choice and
                # are handled by the targeted user-input path in execute().
                raise
            except Exception as exc:
                if attempt >= max_attempts:
                    if recovery:
                        recovery.outcome = "failed"
                        recovery.completed_at = utc_now()
                    raise
                transient = RecoveryAttempt(
                    code="executor_error",
                    category="transient",
                    action="retry_from_untouched_template",
                    attempt=attempt,
                    max_attempts=max_attempts - 1,
                    message=f"The executor failed with {type(exc).__name__}; retrying safely.",
                )
                run.recovery_attempts.append(transient)
                recovery = transient
                run.add_event(
                    "recovery_started",
                    transient.message,
                    recovery_id=transient.id,
                    attempt=transient.attempt,
                    max_attempts=transient.max_attempts,
                )
                staging_output.unlink(missing_ok=True)
                staging_audit.unlink(missing_ok=True)
                continue
            if verification.passed:
                if recovery:
                    recovery.outcome = "succeeded"
                    recovery.completed_at = utc_now()
                    run.add_event(
                        "recovery_succeeded",
                        "The automatic rebuild passed verification.",
                        recovery_id=recovery.id,
                    )
                return verification
            if recovery:
                recovery.outcome = "failed"
                recovery.completed_at = utc_now()
                run.add_event(
                    "recovery_failed",
                    "The automatic rebuild still failed verification.",
                    recovery_id=recovery.id,
                )
        return verification

    def _invalidate_changed_inputs(
        self, run: RunRecord, code: str, message: str
    ) -> RunRecord:
        """Refresh changed inputs autonomously and preserve a still-valid plan."""
        run.approved_spec_hash = None
        run.write_approval = None
        run.error = None
        paths = [Path(run.template.stored_path), *[Path(item.stored_path) for item in run.sources]]
        run.profiles = [inspect_workbook(path) for path in paths]
        run.template.sha256 = sha256_file(Path(run.template.stored_path))
        for source in run.sources:
            source.sha256 = sha256_file(Path(source.stored_path))
        try:
            validate_merge_spec(run.spec, Path(run.template.stored_path))
            run.conflicts = detect_conflicts(
                Path(run.template.stored_path),
                run.sources,
                run.spec,
            )
            structurally_blocked = {
                item.source_id
                for item in run.conflicts
                if item.source_id
                and item.type
                in {"missing_sheet", "schema_mismatch", "duplicate_row_key", "duplicate_source"}
            }
            run.compiled_plan = compile_merge_plan(
                Path(run.template.stored_path),
                [item for item in run.sources if item.id not in structurally_blocked],
                run.spec,
            )
            run.state = (
                RunState.AWAITING_USER_INPUT
                if any(not conflict.resolved for conflict in run.conflicts)
                else RunState.AWAITING_WRITE_APPROVAL
            )
            next_step = "The existing plan remains valid and is ready for a fresh write approval."
        except Exception:
            run.spec = None
            run.spec_hash = None
            run.compiled_plan = None
            run.conflicts = []
            run.state = RunState.FILES_UPLOADED
            next_step = "The previous plan no longer validates and model replanning is required."
        run.add_event(
            "input_change_detected",
            f"{message} Inputs were re-inspected automatically. {next_step}",
            code=code,
        )
        return self.repository.save(run)

    def resolve_decision(
        self,
        run_id: str,
        decision_id: str,
        response: HumanDecisionResponse,
    ) -> RunRecord:
        run = self.get(run_id)
        decision = next((item for item in run.decisions if item.id == decision_id), None)
        if decision is None:
            raise KeyError(decision_id)
        if decision.resolved:
            raise ValueError("This runtime question has already been answered")
        allowed_actions = {item.action for item in decision.options}
        if response.action not in allowed_actions:
            raise ValueError(f"Action {response.action!r} is not allowed for this question")

        if response.action == "exclude_source_and_retry":
            source_id = str(decision.context.get("source_id") or "")
            source_file = str(decision.context.get("source_file") or "")
            source = next(
                (
                    item
                    for item in run.sources
                    if (source_id and item.id == source_id)
                    or (not source_id and item.filename == source_file)
                ),
                None,
            )
            if source is None:
                raise ValueError("This runtime question does not identify an excludable source")
            if source.id not in run.excluded_sources:
                run.excluded_sources.append(source.id)
            run.write_approval = None
            run.approved_spec_hash = None

        decision.selected_action = response.action
        decision.user_note = response.note
        decision.resolved_at = utc_now()
        run.error = None
        if response.action == "abort":
            run.state = RunState.CANCELLED
            message = "The runtime question was answered by aborting the run."
        elif response.action == "return_to_planning":
            run.approved_spec_hash = None
            run.write_approval = None
            run.state = RunState.FILES_UPLOADED
            message = "The runtime question returned the run to model planning."
        else:
            run.state = (
                RunState.PLAN_READY
                if run.write_approval and run.write_approval.spec_hash == run.spec_hash
                else RunState.AWAITING_WRITE_APPROVAL
            )
            message = (
                "The runtime question was resolved; execution can restart safely."
                if run.state == RunState.PLAN_READY
                else "The runtime question changed execution scope; a fresh write approval is required."
            )
        run.add_event(
            "runtime_decision_resolved",
            message,
            decision_id=decision.id,
            action=response.action,
        )
        return self.repository.save(run)
