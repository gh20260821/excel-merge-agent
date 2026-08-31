from app.domain import MergeOperation, MergeSpec, RowFilterRules, StackGroup


STATUS_SHEET = "Status Summary"
PLAN_SHEET = "Project Plan"
AGGREGATE_LABELS = ["Project Type 1", "Project Type 2"]


def representative_fixture_spec() -> MergeSpec:
    return MergeSpec(
        template_family="generic_project_inventory_v1",
        operations=[
            MergeOperation(
                id="status_add",
                sheet=STATUS_SHEET,
                mode="add",
                description="Add status values by equipment label.",
                row_keys=["Category 1", "Category 2"],
                alignment="row_key",
                key_column=1,
                value_columns=[2, 3],
            ),
            MergeOperation(
                id="independent_projects",
                sheet=PLAN_SHEET,
                mode="concatenate",
                description="Append independent project rows in source order.",
                alignment="position",
                key_column=1,
                data_start_row=5,
                end_marker_prefix="Instruction:",
                column_count=25,
                row_filter=RowFilterRules(
                    exclude_prefixes=["Example:"],
                    exclude_exact_values=AGGREGATE_LABELS,
                    exclude_contains=["Replace with Project Name"],
                ),
                style_template_row=6,
                placement="stack",
                stack_group="plan_body",
                stack_order=10,
            ),
            MergeOperation(
                id="aggregate_plan_rows",
                sheet=PLAN_SHEET,
                mode="add",
                description="Add aggregate rows by label and position.",
                row_keys=AGGREGATE_LABELS,
                alignment="row_key",
                key_column=1,
                value_columns=list(range(2, 26)),
                placement="stack",
                stack_group="plan_body",
                stack_order=20,
            ),
        ],
        stack_groups=[
            StackGroup(
                id="plan_body",
                sheet=PLAN_SHEET,
                start_row=5,
                column_count=25,
                end_marker_prefix="Instruction:",
            )
        ],
        rationale="Representative fixed-pattern test configuration.",
        guideline_citations=[f"{STATUS_SHEET}!A8", f"{PLAN_SHEET}!A9"],
    )
