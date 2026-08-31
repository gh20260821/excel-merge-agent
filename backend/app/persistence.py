from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from .domain import RunRecord


class ConcurrentRunUpdate(RuntimeError):
    pass


class RunRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "revision" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_state_updated ON runs(state, updated_at)"
            )
            connection.execute("PRAGMA optimize")

    def save(self, run: RunRecord) -> RunRecord:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT revision FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            previous_revision = run.revision
            run.revision = previous_revision + 1
            payload = run.model_dump_json()
            if existing is None:
                connection.execute(
                    "INSERT INTO runs(id, state, updated_at, payload, revision) VALUES (?, ?, ?, ?, ?)",
                    (run.id, run.state.value, run.updated_at, payload, run.revision),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE runs
                    SET state = ?, updated_at = ?, payload = ?, revision = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (
                        run.state.value,
                        run.updated_at,
                        payload,
                        run.revision,
                        run.id,
                        previous_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    run.revision = previous_revision
                    raise ConcurrentRunUpdate(
                        "Run changed concurrently; reload it before applying this update"
                    )
        return run

    def get(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord.model_validate(json.loads(row["payload"]))

    def list(self, limit: int = 25) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        runs: list[RunRecord] = []
        for row in rows:
            try:
                runs.append(RunRecord.model_validate(json.loads(row["payload"])))
            except (json.JSONDecodeError, ValidationError):
                # Older unfinished plans may no longer satisfy the active schema.
                # Keep one stale row from breaking the complete recent-task list.
                continue
        return runs
