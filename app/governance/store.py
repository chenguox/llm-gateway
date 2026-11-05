"""Small SQLite repository; every database operation runs outside the event loop."""

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import anyio

from app.governance.models import InvocationRecord


class RecordStorageError(Exception):
    pass


class RecordStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def _run(self, operation, *args):
        try:
            return await anyio.to_thread.run_sync(partial(operation, *args))
        except (sqlite3.Error, OSError):
            raise RecordStorageError("Call record storage unavailable") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    async def initialize(self, retention_days: int) -> None:
        await self._run(self._initialize, retention_days)

    def _initialize(self, retention_days: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            for table in ("calls", "attempts"):
                parent = (
                    ", FOREIGN KEY(call_id) REFERENCES calls(id) ON DELETE CASCADE"
                    if table == "attempts"
                    else ""
                )
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    "id TEXT PRIMARY KEY, call_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                    "status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, "
                    "model TEXT, provider TEXT, payload TEXT NOT NULL" + parent + ")"
                )
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {table}_request ON {table}(request_id)"
                )
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {table}_route_time ON {table}(model, started_at)"
                )
            connection.execute("CREATE INDEX IF NOT EXISTS attempts_call ON attempts(call_id)")
            connection.commit()
        finally:
            connection.close()
        self._prune(retention_days)

    def _prune(self, retention_days: int) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM calls WHERE status != 'in_progress' AND ended_at < ?", (cutoff,)
            )
            connection.commit()
        finally:
            connection.close()

    async def maintain(self, retention_days: int) -> None:
        while True:
            await anyio.sleep(3600)
            try:
                await self._run(self._prune, retention_days)
            except RecordStorageError:
                logging.getLogger(__name__).error("CALL_RECORD_RETENTION_FAILED")

    async def create(self, record: InvocationRecord) -> None:
        await self._run(self._write, record.model_copy(deep=True), True)

    async def save(self, record: InvocationRecord) -> None:
        await self._run(self._write, record.model_copy(deep=True), False)

    def _write(self, record: InvocationRecord, create: bool) -> None:
        table = "calls" if record.attempt_number is None else "attempts"
        values = (
            record.call_id,
            record.request_id,
            record.result.status,
            record.timing.started_at.isoformat(),
            record.timing.ended_at.isoformat() if record.timing.ended_at else None,
            record.route.logical_model,
            record.route.provider,
            record.model_dump_json(),
            record.id,
        )
        connection = self._connect()
        try:
            if create:
                connection.execute(
                    f"INSERT INTO {table} "
                    "(call_id, request_id, status, started_at, ended_at, "
                    "model, provider, payload, id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            else:
                connection.execute(
                    f"UPDATE {table} SET call_id=?, request_id=?, status=?, started_at=?, "
                    "ended_at=?, model=?, provider=?, payload=? "
                    "WHERE id=? AND (status='in_progress' OR status=?)",
                    (*values, record.result.status),
                )
            connection.commit()
        finally:
            connection.close()

    async def find_calls(
        self,
        *,
        request_id: str | None = None,
        model: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        return await self._run(self._find, "calls", request_id, model, since, limit)

    async def find_attempts(self, call_id: str) -> list[dict]:
        return await self._run(self._attempts, call_id)

    def _find(
        self, table: str, request_id: str | None, model: str | None, since: str | None, limit: int
    ) -> list[dict]:
        clauses, values = [], []
        for column, value, operator in (
            ("request_id", request_id, "="),
            ("model", model, "="),
            ("started_at", since, ">="),
        ):
            if value is not None:
                clauses.append(f"{column}{operator}?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT payload FROM {table}{where} ORDER BY started_at DESC LIMIT ?",
                (*values, min(max(limit, 1), 1000)),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    def _attempts(self, call_id: str) -> list[dict]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload FROM attempts WHERE call_id=? ORDER BY started_at", (call_id,)
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()
