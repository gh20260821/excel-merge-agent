from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RunState(StrEnum):
    CREATED = "created"
    FILES_UPLOADED = "files_uploaded"
    INSPECTING = "inspecting"
    PLAN_READY = "plan_ready"
    AWAITING_WRITE_APPROVAL = "awaiting_write_approval"
    AWAITING_USER_INPUT = "awaiting_user_input"
    RECOVERING = "recovering"
    # Retained so unfinished records from earlier releases remain readable.
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_HUMAN = "awaiting_human"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UploadedWorkbook(BaseModel):
    id: str = ""
    role: Literal["template", "source"]
    filename: str
    stored_path: str
    sha256: str


class WorkbookSheetProfile(BaseModel):
    name: str
    max_row: int
    max_column: int
    formula_cells: int = 0


class WorkbookProfile(BaseModel):
    filename: str
    sheets: list[WorkbookSheetProfile]


class RowFilterRules(BaseModel):
    exclude_prefixes: list[str] = Field(default_factory=list)
    exclude_exact_values: list[str] = Field(default_factory=list)
    exclude_contains: list[str] = Field(default_factory=list)
    exclude_regexes: list[str] = Field(default_factory=list)

    def matches(self, value: Any) -> bool:
        text = str(value or "").strip()
        return (
            any(text.startswith(prefix) for prefix in self.exclude_prefixes)
            or text in self.exclude_exact_values
            or any(fragment in text for fragment in self.exclude_contains)
            or any(re.search(pattern, text) is not None for pattern in self.exclude_regexes)
        )


class MergeOperation(BaseModel):
    id: str
    sheet: str
    source_sheet: str | None = None
    mode: Literal["add", "concatenate"]
    description: str
    row_keys: list[str] = Field(default_factory=list)
    alignment: Literal["row_key", "position"] = "row_key"
    column_alignment: Literal["auto", "header_path", "position"] = "auto"
    key_column: int | None = Field(default=None, ge=1)
    value_columns: list[int] = Field(default_factory=list)
    data_start_row: int | None = Field(default=None, ge=1)
    end_marker_prefix: str | None = None
    column_count: int | None = Field(default=None, ge=1)
    row_filter: RowFilterRules = Field(default_factory=RowFilterRules)
    style_template_row: int | None = Field(default=None, ge=1)
    placement: Literal["in_place", "stack"] = "in_place"
    stack_group: str | None = None
    stack_order: int = 0

    @property
    def input_sheet(self) -> str:
        return self.source_sheet or self.sheet


class StackGroup(BaseModel):
    id: str
    sheet: str
    start_row: int = Field(ge=1)
    column_count: int = Field(ge=1)
    end_marker_prefix: str | None = None
    retain_end_marker: bool = True


class MergeSpec(BaseModel):
    schema_version: Literal[2] = 2
    template_family: str = "generic_xlsx_v1"
    operations: list[MergeOperation]
    stack_groups: list[StackGroup] = Field(default_factory=list)
    formula_policy: Literal["freeze_displayed_value"] = "freeze_displayed_value"
    blank_numeric_policy: Literal["zero"] = "zero"
    retain_notes: bool = True
    rationale: str = ""
    guideline_citations: list[str] = Field(default_factory=list)

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Conflict(BaseModel):
    id: str
    type: Literal[
        "numeric_text_mismatch",
        "missing_sheet",
        "missing_row_key",
        "schema_mismatch",
        "formula_error",
        "duplicate_row_key",
        "duplicate_source",
    ]
    severity: Literal["blocking", "warning"] = "blocking"
    source_id: str | None = None
    source_file: str
    sheet: str
    cell: str | None = None
    row_key: str | None = None
    expected: str
    actual: Any
    message: str
    recommended_action: str
    allowed_actions: list[str]
    resolution: str | None = None
    resolution_scope: str | None = None

    @property
    def resolved(self) -> bool:
        return self.resolution is not None


class ConflictResolution(BaseModel):
    action: Literal["treat_as_zero", "keep_marker", "skip_cell", "exclude_source", "abort"]
    scope: Literal["this_cell", "this_row", "this_file", "identical_in_run"] = "this_cell"


class DecisionOption(BaseModel):
    action: Literal[
        "retry_execution",
        "exclude_source_and_retry",
        "return_to_planning",
        "abort",
    ]
    label: str
    description: str


class HumanDecisionRequest(BaseModel):
    id: str = Field(default_factory=lambda: f"decision-{uuid.uuid4().hex[:12]}")
    phase: Literal["execution", "verification"]
    code: str
    question: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    options: list[DecisionOption]
    created_at: str = Field(default_factory=utc_now)
    resolved_at: str | None = None
    selected_action: str | None = None
    user_note: str | None = None

    @property
    def resolved(self) -> bool:
        return self.selected_action is not None


class HumanDecisionResponse(BaseModel):
    action: Literal[
        "retry_execution",
        "exclude_source_and_retry",
        "return_to_planning",
        "abort",
    ]
    note: str | None = Field(default=None, max_length=2000)


class WriteApprovalGrant(BaseModel):
    spec_hash: str
    template_sha256: str
    source_sha256s: dict[str, str]
    conflict_resolutions: dict[str, str] = Field(default_factory=dict)
    excluded_sources: list[str] = Field(default_factory=list)
    output_paths: list[str]
    compiled_plan_hash: str | None = None
    batch_size: int = Field(default=50, ge=1, le=500)
    granted_at: str = Field(default_factory=utc_now)


class BatchProgress(BaseModel):
    batch_size: int = Field(ge=1, le=500)
    total_sources: int = Field(ge=0)
    total_work_units: int = Field(ge=0)
    completed_work_units: int = Field(default=0, ge=0)
    current_operation: str | None = None
    current_batch: int = Field(default=0, ge=0)
    batches_in_operation: int = Field(default=0, ge=0)
    processed_sources: int = Field(default=0, ge=0)
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    started_at: str | None = None
    completed_at: str | None = None


class CompiledColumnMapping(BaseModel):
    target_column: int = Field(ge=1)
    source_column: int = Field(ge=1)
    header_path: list[str] = Field(default_factory=list)
    matched_by: Literal["header_path", "position", "key_column"]


class CompiledRowMapping(BaseModel):
    source_row: int = Field(ge=1)
    target_row: int | None = Field(default=None, ge=1)
    row_key: str


class CompiledSourceOperation(BaseModel):
    source_id: str
    source_file: str
    source_sheet: str
    target_sheet: str
    columns: list[CompiledColumnMapping]
    rows: list[CompiledRowMapping]


class CompiledOperation(BaseModel):
    operation_id: str
    mode: Literal["add", "concatenate"]
    sources: list[CompiledSourceOperation]


class CompiledMergePlan(BaseModel):
    spec_hash: str
    compiled_at: str = Field(default_factory=utc_now)
    operations: list[CompiledOperation]

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RecoveryAttempt(BaseModel):
    id: str = Field(default_factory=lambda: f"recovery-{uuid.uuid4().hex[:12]}")
    code: str
    category: Literal["transient", "deterministic", "ambiguous", "plan_change", "fatal"]
    action: str
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    outcome: Literal["started", "succeeded", "failed"] = "started"
    message: str
    created_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None


class VerificationResult(BaseModel):
    passed: bool
    checks: list[dict[str, Any]]


class PlannerProvenance(BaseModel):
    kind: Literal["model", "test"]
    model: str
    evidence_sha256: str
    tool_name: str = "submit_merge_plan"
    attempts: int = Field(default=1, ge=1)
    profile_id: str | None = None
    provider: str | None = None


class RunRecord(BaseModel):
    id: str
    revision: int = Field(default=0, ge=0)
    state: RunState = RunState.CREATED
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    template: UploadedWorkbook | None = None
    sources: list[UploadedWorkbook] = Field(default_factory=list)
    profiles: list[WorkbookProfile] = Field(default_factory=list)
    spec: MergeSpec | None = None
    spec_hash: str | None = None
    compiled_plan: CompiledMergePlan | None = None
    approved_spec_hash: str | None = None
    write_approval: WriteApprovalGrant | None = None
    planner: PlannerProvenance | None = None
    model_profile_id: str | None = None
    conflicts: list[Conflict] = Field(default_factory=list)
    decisions: list[HumanDecisionRequest] = Field(default_factory=list)
    excluded_sources: list[str] = Field(default_factory=list)
    batch_size: int = Field(default=50, ge=1, le=500)
    batch_progress: BatchProgress | None = None
    execution_attempts: int = 0
    recovery_attempts: list[RecoveryAttempt] = Field(default_factory=list)
    output_path: str | None = None
    audit_path: str | None = None
    verification: VerificationResult | None = None
    conversation: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None

    def add_event(self, kind: str, message: str, **data: Any) -> None:
        self.updated_at = utc_now()
        self.events.append(
            {"at": self.updated_at, "kind": kind, "message": message, "data": data}
        )
