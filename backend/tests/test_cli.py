from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from app.cli import Console, _collect_sources, _continue_run, build_parser
from app.domain import ConflictResolution, RunState
from app.persistence import RunRepository
from app.service import RunService
from tests.fixture_specs import representative_fixture_spec
from tests.synthetic_workbooks import build_synthetic_workbooks


def upload(path: Path) -> UploadFile:
    return UploadFile(file=BytesIO(path.read_bytes()), filename=path.name)


def test_merge_parser_accepts_repeated_sources_and_directories(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "merge",
            "--template",
            str(tmp_path / "template.xlsx"),
            "--source",
            str(tmp_path / "one.xlsx"),
            str(tmp_path / "two.xlsx"),
            "--source",
            str(tmp_path / "three.xlsx"),
            "--source-dir",
            str(tmp_path / "sources"),
            "--batch-size",
            "25",
        ]
    )
    assert [path.name for path in args.sources] == ["one.xlsx", "two.xlsx", "three.xlsx"]
    assert args.source_dir == [tmp_path / "sources"]
    assert args.batch_size == 25


def test_source_directory_collection_is_sorted_and_deduplicated(tmp_path: Path) -> None:
    template = tmp_path / "template.xlsx"
    template.touch()
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    first = source_dir / "a.xlsx"
    second = source_dir / "b.xlsx"
    first.touch()
    second.touch()
    args = build_parser().parse_args(
        [
            "merge",
            "--template",
            str(template),
            "--source",
            str(second),
            "--source-dir",
            str(source_dir),
        ]
    )
    assert _collect_sources(args, template.resolve()) == [second.resolve(), first.resolve()]


@pytest.mark.asyncio
async def test_cli_pauses_then_resumes_and_publishes_only_approved_paths(
    tmp_path: Path,
) -> None:
    fixtures = build_synthetic_workbooks(tmp_path / "fixtures")
    service = RunService(RunRepository(tmp_path / "runs.sqlite3"), tmp_path / "runs")
    run = service.create("test-profile")
    run = await service.save_files(
        run.id,
        upload(fixtures / "template.xlsx"),
        [upload(fixtures / "source_1.xlsx"), upload(fixtures / "source_2.xlsx")],
    )
    run = service.inspect_and_plan(run.id, representative_fixture_spec())
    run = service.resolve_conflict(
        run.id,
        run.conflicts[0].id,
        ConflictResolution(action="treat_as_zero", scope="identical_in_run"),
    )
    assert run.state == RunState.AWAITING_WRITE_APPROVAL

    output = tmp_path / "published" / "merged.xlsx"
    audit = tmp_path / "published" / "merged.audit.json"
    paused_console = Console(
        output=StringIO(),
        error=StringIO(),
        input_function=lambda _: "",
    )
    paused = await _continue_run(
        service, None, run.id, output, audit, paused_console
    )
    assert paused == 2
    assert service.get(run.id).state == RunState.AWAITING_WRITE_APPROVAL
    assert not output.exists()

    resumed_console = Console(
        output=StringIO(),
        error=StringIO(),
        input_function=lambda _: "approve",
    )
    completed = await _continue_run(
        service, None, run.id, output, audit, resumed_console
    )
    assert completed == 0
    completed_run = service.get(run.id)
    assert completed_run.state == RunState.COMPLETED
    assert completed_run.verification and completed_run.verification.passed
    assert output.exists()
    assert audit.exists()
    assert str(output.resolve()) in completed_run.write_approval.output_paths
    assert str(audit.resolve()) in completed_run.write_approval.output_paths

    with pytest.raises(ValueError, match="not included in the write approval"):
        service.publish_outputs(run.id, tmp_path / "unapproved.xlsx")
