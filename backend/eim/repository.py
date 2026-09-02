"""EIM SQLite 仓储、迁移与原子状态变更。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.eim.models import (
    BuildState,
    CanonicalEvent,
    DeliveryState,
    DesiredState,
    EIMConnection,
    EIMDestination,
    EIMTask,
    EIMTaskVersion,
    EventType,
    ObservedState,
    ensure_editable,
    new_ulid,
    utc_now,
    validate_build_transition,
    validate_observed_transition,
)
from backend.eim.redaction import redact_structure, sanitize_payload


_SCHEMA_VERSION = 5


def _json(value: Any) -> str:
    """稳定编码 JSON，便于哈希、导出和差异比较。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EIMRepository:
    """单写锁配合 SQLite WAL，保证桌面后台线程共享同一事实来源。"""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._migrate()
        self.recover_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=15.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _migrate(self) -> None:
        """迁移只向前创建，不自动删除未知旧字段或业务数据。"""

        with self._write_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS eim_schema_version (
                    version INTEGER NOT NULL
                );
                INSERT INTO eim_schema_version(version)
                SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM eim_schema_version);

                CREATE TABLE IF NOT EXISTS eim_connections (
                    connection_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    connection_state TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    checked_at TEXT,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eim_destinations (
                    destination_id TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL,
                    destination_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    checked_at TEXT,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(connection_id) REFERENCES eim_connections(connection_id)
                );

                CREATE TABLE IF NOT EXISTS eim_tasks (
                    task_id TEXT PRIMARY KEY,
                    display_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    build_state TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    observed_state TEXT NOT NULL,
                    active_version_id TEXT,
                    draft_revision INTEGER NOT NULL,
                    deleted_at TEXT,
                    last_activity_at TEXT,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(connection_id) REFERENCES eim_connections(connection_id),
                    FOREIGN KEY(destination_id) REFERENCES eim_destinations(destination_id)
                );
                CREATE INDEX IF NOT EXISTS idx_eim_tasks_states
                    ON eim_tasks(deleted_at, desired_state, observed_state);

                CREATE TABLE IF NOT EXISTS eim_task_versions (
                    version_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(task_id, content_hash),
                    FOREIGN KEY(task_id) REFERENCES eim_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS eim_samples (
                    sample_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES eim_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS eim_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    exit_reason TEXT,
                    heartbeat_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES eim_tasks(task_id) ON DELETE CASCADE,
                    FOREIGN KEY(version_id) REFERENCES eim_task_versions(version_id)
                );

                CREATE TABLE IF NOT EXISTS eim_event_inbox (
                    inbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    processing_state TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    last_error TEXT,
                    FOREIGN KEY(task_id) REFERENCES eim_tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_eim_inbox_task_state
                    ON eim_event_inbox(task_id, processing_state, inbox_id);

                CREATE TABLE IF NOT EXISTS eim_delivery_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    action_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    next_retry_at TEXT,
                    external_ref TEXT,
                    last_error TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES eim_tasks(task_id) ON DELETE CASCADE,
                    FOREIGN KEY(version_id) REFERENCES eim_task_versions(version_id),
                    FOREIGN KEY(destination_id) REFERENCES eim_destinations(destination_id)
                );
                CREATE INDEX IF NOT EXISTS idx_eim_outbox_due
                    ON eim_delivery_outbox(state, next_retry_at, created_at);

                CREATE TABLE IF NOT EXISTS eim_ai_sessions (
                    session_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    configuration_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    steps INTEGER NOT NULL DEFAULT 0,
                    usage_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES eim_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS eim_ai_config_health (
                    configuration_id TEXT PRIMARY KEY,
                    compatible INTEGER NOT NULL,
                    detail TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eim_ai_usage (
                    usage_day TEXT NOT NULL,
                    configuration_id TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    calls INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(usage_day, configuration_id, unit)
                );

                CREATE TABLE IF NOT EXISTS eim_audit_log (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    task_id TEXT,
                    version_id TEXT,
                    result TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eim_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    event_id TEXT,
                    message_id TEXT,
                    stage TEXT NOT NULL,
                    result TEXT NOT NULL,
                    external_ref TEXT,
                    preview TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_eim_logs_filter
                    ON eim_logs(task_id, timestamp, stage, result);

                UPDATE eim_schema_version SET version = 3 WHERE version < 3;
                COMMIT;
                """
            )
            version = int(
                connection.execute("SELECT version FROM eim_schema_version").fetchone()[0]
            )
            if version > _SCHEMA_VERSION:
                raise RuntimeError("EIM 数据库版本高于当前程序，拒绝降级写入")
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(eim_event_inbox)")
            }
            for name, declaration in (
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("next_retry_at", "TEXT"),
                ("last_error", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE eim_event_inbox ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_eim_inbox_due "
                "ON eim_event_inbox(processing_state, next_retry_at, inbox_id)"
            )
            connection.execute("UPDATE eim_schema_version SET version = 4 WHERE version < 4")
            if "occurred_at" not in columns:
                connection.execute("ALTER TABLE eim_event_inbox ADD COLUMN occurred_at TEXT")
                connection.execute(
                    "UPDATE eim_event_inbox SET occurred_at=received_at WHERE occurred_at IS NULL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_eim_inbox_context "
                "ON eim_event_inbox(task_id, occurred_at, inbox_id)"
            )
            connection.execute("UPDATE eim_schema_version SET version = 5 WHERE version < 5")

    @staticmethod
    def _connection_from_row(row: sqlite3.Row | None) -> EIMConnection | None:
        return EIMConnection.model_validate_json(row["payload"]) if row else None

    @staticmethod
    def _destination_from_row(row: sqlite3.Row | None) -> EIMDestination | None:
        return EIMDestination.model_validate_json(row["payload"]) if row else None

    @staticmethod
    def _task_from_row(row: sqlite3.Row | None) -> EIMTask | None:
        return EIMTask.model_validate_json(row["payload"]) if row else None

    @staticmethod
    def _version_from_row(row: sqlite3.Row | None) -> EIMTaskVersion | None:
        return EIMTaskVersion.model_validate_json(row["payload"]) if row else None

    def save_connection(self, value: EIMConnection) -> EIMConnection:
        value.updated_at = utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO eim_connections(
                    connection_id, platform, connection_state, profile, checked_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    platform=excluded.platform,
                    connection_state=excluded.connection_state,
                    profile=excluded.profile,
                    checked_at=excluded.checked_at,
                    payload=excluded.payload
                """,
                (
                    value.connection_id,
                    value.platform,
                    value.connection_state,
                    value.profile,
                    value.checked_at,
                    value.model_dump_json(),
                ),
            )
        return value

    def get_connection(self, connection_id: str) -> EIMConnection | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM eim_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
        return self._connection_from_row(row)

    def list_connections(self) -> list[EIMConnection]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM eim_connections ORDER BY connection_id"
            ).fetchall()
        return [EIMConnection.model_validate_json(row["payload"]) for row in rows]

    def save_destination(self, value: EIMDestination) -> EIMDestination:
        if self.get_connection(value.connection_id) is None:
            raise ValueError("目标引用的 EIM 连接不存在")
        value.updated_at = utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO eim_destinations(
                    destination_id, connection_id, destination_type, url,
                    checked_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(destination_id) DO UPDATE SET
                    connection_id=excluded.connection_id,
                    destination_type=excluded.destination_type,
                    url=excluded.url,
                    checked_at=excluded.checked_at,
                    payload=excluded.payload
                """,
                (
                    value.destination_id,
                    value.connection_id,
                    value.destination_type,
                    value.url,
                    value.checked_at,
                    value.model_dump_json(),
                ),
            )
        return value

    def get_destination(self, destination_id: str) -> EIMDestination | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM eim_destinations WHERE destination_id = ?",
                (destination_id,),
            ).fetchone()
        return self._destination_from_row(row)

    def create_task(self, value: EIMTask) -> EIMTask:
        if self.get_connection(value.connection_id) is None:
            raise ValueError("任务引用的 EIM 连接不存在")
        destination = self.get_destination(value.destination_id)
        if destination is None or destination.connection_id != value.connection_id:
            raise ValueError("任务目标不存在或不属于当前连接")
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO eim_tasks(
                        task_id, display_id, name, connection_id, destination_id,
                        build_state, desired_state, observed_state, active_version_id,
                        draft_revision, deleted_at, last_activity_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._task_values(value),
                )
                self._append_audit_row(
                    connection,
                    actor="user",
                    action="task.create",
                    task_id=value.task_id,
                    result="success",
                    details={"display_id": value.display_id},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return value

    @staticmethod
    def _task_values(value: EIMTask) -> tuple[Any, ...]:
        return (
            value.task_id,
            value.display_id,
            value.name,
            value.connection_id,
            value.destination_id,
            value.build_state,
            value.desired_state,
            value.observed_state,
            value.active_version_id,
            value.draft_revision,
            value.deleted_at,
            value.last_activity_at,
            value.model_dump_json(),
        )

    def _save_task(self, connection: sqlite3.Connection, value: EIMTask) -> None:
        value.updated_at = utc_now()
        cursor = connection.execute(
            """
            UPDATE eim_tasks SET
                display_id=?, name=?, connection_id=?, destination_id=?,
                build_state=?, desired_state=?, observed_state=?, active_version_id=?,
                draft_revision=?, deleted_at=?, last_activity_at=?, payload=?
            WHERE task_id=?
            """,
            self._task_values(value)[1:] + (value.task_id,),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"EIM 任务不存在：{value.task_id}")

    def get_task(self, task_id: str) -> EIMTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM eim_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row)

    def list_tasks(self, *, include_deleted: bool = False) -> list[EIMTask]:
        where = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload FROM eim_tasks {where} ORDER BY display_id DESC"
            ).fetchall()
        return [EIMTask.model_validate_json(row["payload"]) for row in rows]

    def update_task_draft(
        self,
        task_id: str,
        *,
        name: str | None = None,
        source_id: str | None = None,
        source_name: str | None = None,
        destination_id: str | None = None,
        event_types: list[EventType] | None = None,
    ) -> EIMTask:
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = self._required_task(connection, task_id)
                ensure_editable(task)
                if task.deleted_at:
                    raise ValueError("回收站中的任务不能编辑")
                if name is not None:
                    task.name = name.strip()
                if source_id is not None:
                    task.source_id = source_id.strip()
                if source_name is not None:
                    task.source_name = source_name.strip()
                if destination_id is not None:
                    destination = self.get_destination(destination_id)
                    if destination is None or destination.connection_id != task.connection_id:
                        raise ValueError("归档目标不存在或不属于当前连接")
                    task.destination_id = destination_id
                if event_types is not None:
                    task.event_types = list(event_types)
                task.draft_revision += 1
                task.build_state = BuildState.DRAFT
                self._save_task(connection, task)
                self._append_audit_row(
                    connection,
                    actor="user",
                    action="task.edit",
                    task_id=task_id,
                    result="success",
                    details={"draft_revision": task.draft_revision},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return task

    def transition_build(self, task_id: str, target: BuildState) -> EIMTask:
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = self._required_task(connection, task_id)
                ensure_editable(task)
                validate_build_transition(task.build_state, target)
                task.build_state = target
                self._save_task(connection, task)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return task

    def transition_observed(
        self,
        task_id: str,
        target: ObservedState,
        *,
        desired: DesiredState | None = None,
    ) -> EIMTask:
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = self._required_task(connection, task_id)
                validate_observed_transition(task.observed_state, target)
                task.observed_state = target
                if desired is not None:
                    task.desired_state = desired
                self._save_task(connection, task)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return task

    def touch_task(self, task_id: str, timestamp: str | None = None) -> None:
        with self._write_lock, self._connect() as connection:
            task = self._required_task(connection, task_id)
            task.last_activity_at = timestamp or utc_now()
            self._save_task(connection, task)

    def copy_task(self, task_id: str, *, name: str | None = None) -> EIMTask:
        source = self.get_task(task_id)
        if source is None:
            raise KeyError(f"EIM 任务不存在：{task_id}")
        ensure_editable(source)
        copied = EIMTask(
            name=(name or f"{source.name} - 副本").strip(),
            connection_id=source.connection_id,
            source_id=source.source_id,
            source_name=source.source_name,
            destination_id=source.destination_id,
            event_types=list(source.event_types),
        )
        return self.create_task(copied)

    def soft_delete_task(self, task_id: str) -> EIMTask:
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = self._required_task(connection, task_id)
                ensure_editable(task)
                task.deleted_at = utc_now()
                task.desired_state = DesiredState.STOPPED
                self._save_task(connection, task)
                self._append_audit_row(
                    connection,
                    actor="user",
                    action="task.soft_delete",
                    task_id=task_id,
                    result="success",
                    details={},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return task

    def restore_task(self, task_id: str) -> EIMTask:
        with self._write_lock, self._connect() as connection:
            task = self._required_task(connection, task_id)
            if not task.deleted_at:
                raise ValueError("EIM 任务不在回收站中")
            task.deleted_at = None
            task.desired_state = DesiredState.STOPPED
            task.observed_state = ObservedState.STOPPED
            self._save_task(connection, task)
        return task

    def purge_task(self, task_id: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._required_task(connection, task_id)
            try:
                ensure_editable(task)
                if not task.deleted_at:
                    raise ValueError("只有回收站中的 EIM 任务才能永久删除")
                connection.execute("DELETE FROM eim_logs WHERE task_id = ?", (task_id,))
                connection.execute("DELETE FROM eim_audit_log WHERE task_id = ?", (task_id,))
                connection.execute("DELETE FROM eim_tasks WHERE task_id = ?", (task_id,))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def publish_version(
        self,
        version: EIMTaskVersion,
        *,
        expected_draft_revision: int,
        staged_samples: list[dict[str, Any]] | None = None,
    ) -> EIMTask:
        """版本写入、旧版本降级和 active 切换在同一事务完成。"""

        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = self._required_task(connection, version.task_id)
                ensure_editable(task)
                if task.draft_revision != expected_draft_revision:
                    raise ValueError("任务草稿已被其他编辑覆盖")
                if task.build_state not in {BuildState.VALIDATING, BuildState.READY}:
                    raise ValueError("任务尚未完成验证，不能发布版本")
                connection.execute(
                    "UPDATE eim_task_versions SET status='superseded' WHERE task_id=? AND status='ready'",
                    (task.task_id,),
                )
                connection.execute(
                    """
                    INSERT INTO eim_task_versions(
                        version_id, task_id, content_hash, status, created_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.version_id,
                        version.task_id,
                        version.content_hash,
                        version.status,
                        version.created_at,
                        version.model_dump_json(),
                    ),
                )
                for sample in staged_samples or []:
                    input_value = sample.get("input")
                    expected_value = sample.get("expected")
                    if not isinstance(input_value, dict) or not isinstance(expected_value, dict):
                        raise ValueError("待发布 EIM 样例格式不合法")
                    connection.execute(
                        "INSERT INTO eim_samples VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            new_ulid(),
                            task.task_id,
                            str(sample.get("source") or "ai")[:40],
                            _json(redact_structure(input_value)),
                            _json(redact_structure(expected_value)),
                            utc_now(),
                        ),
                    )
                task.active_version_id = version.version_id
                task.build_state = BuildState.READY
                self._save_task(connection, task)
                self._append_audit_row(
                    connection,
                    actor="builder",
                    action="version.publish",
                    task_id=task.task_id,
                    version_id=version.version_id,
                    result="success",
                    details={"content_hash": version.content_hash},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return task

    def activate_version(
        self,
        task_id: str,
        version_id: str,
        *,
        expected_draft_revision: int,
    ) -> EIMTask:
        """重新部署既有不可变版本，不复制版本或运行记录。"""

        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = self._required_task(connection, task_id)
                ensure_editable(task)
                if task.draft_revision != expected_draft_revision:
                    raise ValueError("任务草稿已被其他编辑覆盖")
                row = connection.execute(
                    "SELECT version_id FROM eim_task_versions WHERE task_id=? AND version_id=?",
                    (task_id, version_id),
                ).fetchone()
                if row is None:
                    raise ValueError("待部署版本不属于当前任务")
                connection.execute(
                    "UPDATE eim_task_versions SET status='superseded' WHERE task_id=? AND status='ready'",
                    (task_id,),
                )
                connection.execute(
                    "UPDATE eim_task_versions SET status='ready' WHERE version_id=?",
                    (version_id,),
                )
                task.active_version_id = version_id
                task.build_state = BuildState.READY
                self._save_task(connection, task)
                self._append_audit_row(
                    connection,
                    actor="builder",
                    action="version.activate",
                    task_id=task_id,
                    version_id=version_id,
                    result="success",
                    details={},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return task

    def get_version(self, version_id: str) -> EIMTaskVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, payload FROM eim_task_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
        return self._version_with_status(row) if row else None

    def find_version_by_hash(self, task_id: str, content_hash: str) -> EIMTaskVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, payload FROM eim_task_versions
                WHERE task_id=? AND content_hash=?
                """,
                (task_id, content_hash),
            ).fetchone()
        return self._version_with_status(row) if row else None

    def list_versions(self, task_id: str) -> list[EIMTaskVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, payload FROM eim_task_versions WHERE task_id=? ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
        return [self._version_with_status(row) for row in rows]

    @staticmethod
    def _version_with_status(row: sqlite3.Row) -> EIMTaskVersion:
        """状态列是查询事实来源，避免旧版本 JSON 快照显示为 ready。"""

        version = EIMTaskVersion.model_validate_json(row["payload"])
        version.status = row["status"]
        return version

    def add_sample(
        self,
        task_id: str,
        input_value: dict[str, Any],
        expected_value: dict[str, Any],
        *,
        source: str = "manual",
    ) -> str:
        sample_id = new_ulid()
        with self._write_lock, self._connect() as connection:
            self._required_task(connection, task_id)
            connection.execute(
                "INSERT INTO eim_samples VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sample_id,
                    task_id,
                    source,
                    _json(redact_structure(input_value)),
                    _json(redact_structure(expected_value)),
                    utc_now(),
                ),
            )
        return sample_id

    def save_ai_configuration_health(
        self,
        configuration_id: str,
        *,
        compatible: bool,
        detail: str,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO eim_ai_config_health(configuration_id, compatible, detail, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(configuration_id) DO UPDATE SET
                    compatible=excluded.compatible,
                    detail=excluded.detail,
                    checked_at=excluded.checked_at
                """,
                (configuration_id[:128], int(compatible), detail[:500], utc_now()),
            )

    def list_ai_configuration_health(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM eim_ai_config_health ORDER BY configuration_id"
            ).fetchall()
        return [
            {
                "configuration_id": row["configuration_id"],
                "compatible": bool(row["compatible"]),
                "detail": row["detail"],
                "checked_at": row["checked_at"],
            }
            for row in rows
        ]

    def start_ai_session(self, task_id: str, configuration_id: str) -> str:
        session_id = new_ulid()
        now = utc_now()
        with self._write_lock, self._connect() as connection:
            self._required_task(connection, task_id)
            connection.execute(
                """
                INSERT INTO eim_ai_sessions(
                    session_id, task_id, configuration_id, state, steps,
                    usage_json, summary, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', 0, '{}', '', ?, ?)
                """,
                (session_id, task_id, configuration_id[:128], now, now),
            )
        return session_id

    def update_ai_session(
        self,
        session_id: str,
        *,
        state: str,
        steps: int,
        usage: dict[str, Any],
        summary: str,
    ) -> None:
        if state not in {"running", "completed", "failed", "cancelled"}:
            raise ValueError("AI 构建会话状态不合法")
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE eim_ai_sessions
                SET state=?, steps=?, usage_json=?, summary=?, updated_at=?
                WHERE session_id=?
                """,
                (
                    state,
                    max(0, min(int(steps), 12)),
                    _json(sanitize_payload(usage)),
                    str(redact_structure(summary))[:500],
                    utc_now(),
                    session_id,
                ),
            )

    def ai_usage_today(self, configuration_id: str, unit: str) -> dict[str, float | int]:
        day = utc_now()[:10]
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT amount, calls FROM eim_ai_usage
                WHERE usage_day=? AND configuration_id=? AND unit=?
                """,
                (day, configuration_id, unit),
            ).fetchone()
        return {
            "amount": float(row["amount"]) if row else 0.0,
            "calls": int(row["calls"]) if row else 0,
        }

    def add_ai_usage(self, configuration_id: str, unit: str, amount: float) -> None:
        if unit not in {"tokens", "calls"} or amount < 0:
            raise ValueError("AI 用量单位或数值不合法")
        day = utc_now()[:10]
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO eim_ai_usage(usage_day, configuration_id, unit, amount, calls)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(usage_day, configuration_id, unit) DO UPDATE SET
                    amount=amount+excluded.amount,
                    calls=calls+1
                """,
                (day, configuration_id[:128], unit, float(amount)),
            )

    def list_samples(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM eim_samples WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return [
            {
                "sample_id": row["sample_id"],
                "task_id": row["task_id"],
                "source": row["source"],
                "input": json.loads(row["input_json"]),
                "expected": json.loads(row["expected_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def start_run(self, task_id: str, version_id: str) -> str:
        run_id = new_ulid()
        now = utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO eim_runs VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                (run_id, task_id, version_id, now, now),
            )
        return run_id

    def heartbeat_run(self, run_id: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE eim_runs SET heartbeat_at=? WHERE run_id=? AND stopped_at IS NULL",
                (utc_now(), run_id),
            )

    def stop_run(self, run_id: str, reason: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE eim_runs SET stopped_at=?, exit_reason=? WHERE run_id=?",
                (utc_now(), reason[:120], run_id),
            )

    def insert_event(self, task_id: str, event: CanonicalEvent) -> bool:
        """利用唯一 dedupe_key 实现并发安全的至少一次接入去重。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO eim_event_inbox(
                    task_id, event_id, message_id, event_type, received_at, occurred_at,
                    payload_json, processing_state, dedupe_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?)
                """,
                (
                    task_id,
                    event.event_id,
                    event.message_id,
                    event.event_type,
                    event.received_at,
                    event.occurred_at,
                    _json(sanitize_payload(event.model_dump(mode="json"))),
                    f"{task_id}:{event.dedupe_key}",
                ),
            )
        return cursor.rowcount == 1

    def defer_event(self, task_id: str, event_id: str, until: str) -> None:
        """把已领取事件无损放回队列，等待上下文窗口结束。"""

        deadline = datetime.fromisoformat(str(until)).astimezone(UTC)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE eim_event_inbox
                SET processing_state='received', next_retry_at=?, last_error=NULL
                WHERE task_id=? AND event_id=? AND processing_state='processing'
                """,
                (deadline.isoformat(timespec="milliseconds"), task_id, event_id),
            )

    def list_context_events(
        self,
        task_id: str,
        *,
        conversation_id: str,
        sender_id: str,
        occurred_after: str,
        occurred_before: str,
        exclude_event_id: str,
        limit: int,
    ) -> list[CanonicalEvent]:
        """读取同一会话和发送人的邻近事件，供附件关联使用。"""

        if not sender_id or limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM eim_event_inbox
                WHERE task_id=? AND occurred_at>=? AND occurred_at<=?
                  AND event_id<>? AND payload_json<>'{}'
                  AND json_extract(payload_json, '$.conversation_id')=?
                  AND json_extract(payload_json, '$.sender_id')=?
                ORDER BY occurred_at, inbox_id LIMIT ?
                """,
                (
                    task_id,
                    occurred_after,
                    occurred_before,
                    exclude_event_id,
                    conversation_id,
                    sender_id,
                    max(1, min(int(limit), 50)),
                ),
            ).fetchall()
        events: list[CanonicalEvent] = []
        for row in rows:
            try:
                event = CanonicalEvent.model_validate_json(row["payload_json"])
            except Exception:
                continue
            if event.conversation_id == conversation_id and event.sender_id == sender_id:
                events.append(event)
        return events

    def set_event_state(self, task_id: str, event_id: str, state: str) -> None:
        if state not in {"received", "processing", "completed", "failed", "dead_letter"}:
            raise ValueError("未知事件处理状态")
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE eim_event_inbox SET processing_state=? WHERE task_id=? AND event_id=?",
                (state, task_id, event_id),
            )

    def claim_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """原子领取待处理事件，供崩溃恢复和后台调度使用。"""

        claimed: list[dict[str, Any]] = []
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT i.inbox_id, i.task_id, i.event_id, i.payload_json, i.attempts
                    FROM eim_event_inbox AS i
                    JOIN eim_tasks AS t ON t.task_id=i.task_id
                    WHERE i.processing_state='received'
                      AND (i.next_retry_at IS NULL OR i.next_retry_at <= ?)
                      AND t.deleted_at IS NULL
                      AND t.desired_state='running'
                      AND t.observed_state IN ('starting', 'running', 'reconnecting', 'degraded')
                    ORDER BY i.inbox_id LIMIT ?
                    """,
                    (utc_now(), max(1, min(int(limit), 500))),
                ).fetchall()
                for row in rows:
                    cursor = connection.execute(
                        """
                        UPDATE eim_event_inbox SET processing_state='processing'
                        WHERE inbox_id=? AND processing_state='received'
                        """,
                        (row["inbox_id"],),
                    )
                    if cursor.rowcount == 1:
                        claimed.append(
                            {
                                "inbox_id": row["inbox_id"],
                                "task_id": row["task_id"],
                                "event_id": row["event_id"],
                                "attempts": int(row["attempts"] or 0),
                                "event": json.loads(row["payload_json"]),
                            }
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return claimed

    def schedule_event_retry(
        self,
        task_id: str,
        event_id: str,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> str:
        """为事件设置有界指数退避；超过预算后转入可人工恢复的死信。"""

        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT attempts, received_at FROM eim_event_inbox WHERE task_id=? AND event_id=?",
                    (task_id, event_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"EIM 事件不存在：{event_id}")
                attempts = int(row["attempts"] or 0) + 1
                try:
                    received = datetime.fromisoformat(str(row["received_at"])).astimezone(UTC)
                except ValueError:
                    received = datetime.min.replace(tzinfo=UTC)
                exhausted = attempts >= max(1, int(max_attempts)) or (
                    datetime.now(UTC) - received >= timedelta(hours=24)
                )
                state = "dead_letter" if exhausted else "received"
                next_retry = None
                if not exhausted:
                    delay = min(300, 2 ** min(attempts, 8))
                    next_retry = (
                        datetime.now(UTC) + timedelta(seconds=delay)
                    ).isoformat(timespec="milliseconds")
                connection.execute(
                    """
                    UPDATE eim_event_inbox
                    SET processing_state=?, attempts=?, next_retry_at=?, last_error=?
                    WHERE task_id=? AND event_id=?
                    """,
                    (
                        state,
                        attempts,
                        next_retry,
                        str(redact_structure(error))[:1000],
                        task_id,
                        event_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return state

    def list_dead_events(
        self,
        *,
        task_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["processing_state='dead_letter'"]
        values: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            values.append(task_id)
        values.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT inbox_id, task_id, event_id, attempts, last_error, received_at "
                f"FROM eim_event_inbox WHERE {' AND '.join(clauses)} "
                "ORDER BY inbox_id DESC LIMIT ?",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_dead_event(self, inbox_id: int) -> bool:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE eim_event_inbox
                SET processing_state='received', next_retry_at=NULL, last_error=NULL
                WHERE inbox_id=? AND processing_state='dead_letter'
                """,
                (int(inbox_id),),
            )
        return cursor.rowcount == 1

    def enqueue_delivery(
        self,
        *,
        task_id: str,
        version_id: str,
        event_id: str,
        destination_id: str,
        action_name: str,
        payload: dict[str, Any],
    ) -> str:
        idempotency_key = ":".join(
            (task_id, version_id, destination_id, event_id, action_name)
        )
        delivery_id = new_ulid()
        now = utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO eim_delivery_outbox(
                    delivery_id, task_id, version_id, event_id, destination_id,
                    action_name, idempotency_key, attempts, state, next_retry_at,
                    external_ref, last_error, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    delivery_id,
                    task_id,
                    version_id,
                    event_id,
                    destination_id,
                    action_name,
                    idempotency_key,
                    DeliveryState.PENDING,
                    _json(payload),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT delivery_id FROM eim_delivery_outbox WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return str(row["delivery_id"])

    def list_due_deliveries(self, now: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        timestamp = now or utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM eim_delivery_outbox
                WHERE state IN ('pending', 'retry', 'commit_unknown')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at LIMIT ?
                """,
                (timestamp, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._delivery_view(row) for row in rows]

    def claim_due_deliveries(
        self,
        now: str | None = None,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """在同一写事务中领取 outbox，避免多个后台循环重复写目标。"""

        timestamp = now or utc_now()
        claimed: list[dict[str, Any]] = []
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT o.* FROM eim_delivery_outbox AS o
                    JOIN eim_tasks AS t ON t.task_id=o.task_id
                    WHERE o.state IN ('pending', 'retry', 'commit_unknown')
                      AND (o.next_retry_at IS NULL OR o.next_retry_at <= ?)
                      AND t.deleted_at IS NULL
                      AND t.desired_state='running'
                      AND t.observed_state IN ('starting', 'running', 'reconnecting', 'degraded')
                    ORDER BY o.created_at LIMIT ?
                    """,
                    (timestamp, max(1, min(int(limit), 500))),
                ).fetchall()
                for row in rows:
                    previous = str(row["state"])
                    cursor = connection.execute(
                        """
                        UPDATE eim_delivery_outbox SET state='delivering', updated_at=?
                        WHERE delivery_id=? AND state=?
                        """,
                        (utc_now(), row["delivery_id"], previous),
                    )
                    if cursor.rowcount == 1:
                        value = self._delivery_view(row)
                        value["previous_state"] = previous
                        claimed.append(value)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return claimed

    @staticmethod
    def _delivery_view(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def update_delivery(
        self,
        delivery_id: str,
        state: DeliveryState,
        *,
        external_ref: str | None = None,
        last_error: str | None = None,
        next_retry_at: str | None = None,
        increment_attempt: bool = True,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE eim_delivery_outbox SET
                    state=?, attempts=attempts+?, next_retry_at=?, external_ref=?,
                    last_error=?, updated_at=?
                WHERE delivery_id=?
                """,
                (
                    state,
                    1 if increment_attempt else 0,
                    next_retry_at,
                    external_ref,
                    (last_error or "")[:1000] or None,
                    utc_now(),
                    delivery_id,
                ),
            )

    def update_delivery_payload(self, delivery_id: str, payload: dict[str, Any]) -> None:
        """保存媒体上传进度，避免目标写入重试时重复上传远端附件。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE eim_delivery_outbox SET payload_json=?, updated_at=?
                WHERE delivery_id=? AND state='delivering'
                """,
                (_json(payload), utc_now(), delivery_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("EIM 投递媒体进度保存失败")

    def retry_dead_letter(self, delivery_id: str) -> bool:
        """人工重试只改变指定死信，不重置历史尝试次数。"""

        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE eim_delivery_outbox
                SET state='retry', next_retry_at=NULL, last_error=NULL, updated_at=?
                WHERE delivery_id=? AND state='dead_letter'
                """,
                (utc_now(), delivery_id),
            )
        return cursor.rowcount == 1

    def list_dead_letters(
        self,
        *,
        task_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """返回可人工重试的死信；消息正文仍由统一脱敏层限制展示。"""

        clauses = ["state='dead_letter'"]
        values: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            values.append(task_id)
        values.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM eim_delivery_outbox WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._delivery_view(row) for row in rows]

    def list_media_retention_events(self, task_id: str) -> list[dict[str, Any]]:
        """返回事件是否仍有未完成投递，供媒体保留策略选择 24 小时或 30 天。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT inbox.event_id,
                       MAX(CASE WHEN outbox.state IS NOT NULL AND outbox.state != 'completed'
                                THEN 1 ELSE 0 END) AS has_incomplete_delivery
                FROM eim_event_inbox AS inbox
                LEFT JOIN eim_delivery_outbox AS outbox
                  ON outbox.task_id=inbox.task_id AND outbox.event_id=inbox.event_id
                WHERE inbox.task_id=?
                GROUP BY inbox.event_id
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "event_id": str(row["event_id"]),
                "has_incomplete_delivery": bool(row["has_incomplete_delivery"]),
            }
            for row in rows
        ]

    def append_log(
        self,
        *,
        task_id: str | None,
        stage: str,
        result: str,
        event_id: str | None = None,
        message_id: str | None = None,
        external_ref: str | None = None,
        preview: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO eim_logs(
                    task_id, event_id, message_id, stage, result, external_ref,
                    preview, details_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    event_id,
                    message_id,
                    stage[:80],
                    result[:40],
                    external_ref,
                    str(redact_structure(preview))[:240],
                    _json(redact_structure(details or {})),
                    utc_now(),
                ),
            )

    def list_logs(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        stage: str | None = None,
        result: str | None = None,
        since: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("task_id", task_id),
            ("stage", stage),
            ("result", result),
        ):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        if since:
            clauses.append("timestamp >= ?")
            values.append(since)
        if event_type:
            clauses.append(
                "EXISTS (SELECT 1 FROM eim_event_inbox AS inbox "
                "WHERE inbox.task_id=eim_logs.task_id "
                "AND inbox.event_id=eim_logs.event_id "
                "AND inbox.event_type=?)"
            )
            values.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit), 5000)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM eim_logs {where} ORDER BY log_id DESC LIMIT ?",
                values,
            ).fetchall()
        result_rows: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["details"] = json.loads(value.pop("details_json"))
            result_rows.append(value)
        return result_rows

    def append_audit(
        self,
        *,
        actor: str,
        action: str,
        result: str,
        task_id: str | None = None,
        version_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            self._append_audit_row(
                connection,
                actor=actor,
                action=action,
                task_id=task_id,
                version_id=version_id,
                result=result,
                details=details or {},
            )

    @staticmethod
    def _append_audit_row(
        connection: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        result: str,
        task_id: str | None,
        details: dict[str, Any],
        version_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO eim_audit_log(
                actor, action, task_id, version_id, result, timestamp, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor[:40],
                action[:100],
                task_id,
                version_id,
                result[:40],
                utc_now(),
                _json(redact_structure(details)),
            ),
        )

    def recover_interrupted(self) -> int:
        """崩溃后的瞬时状态统一落为 stopped_app_exit，运行意图保持不变。"""

        recovered = 0
        with self._write_lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM eim_tasks
                WHERE observed_state IN ('starting', 'running', 'reconnecting', 'degraded', 'stopping')
                """
            ).fetchall()
            for row in rows:
                task = EIMTask.model_validate_json(row["payload"])
                task.observed_state = ObservedState.STOPPED_APP_EXIT
                self._save_task(connection, task)
                recovered += 1
            if recovered:
                now = utc_now()
                connection.execute(
                    """
                    UPDATE eim_runs SET stopped_at=?, exit_reason='interrupted'
                    WHERE stopped_at IS NULL
                    """,
                    (now,),
                )
            # inbox/outbox 的处理中状态可能恰好在副作用前后崩溃；分别回到可重做和先回读状态。
            connection.execute(
                "UPDATE eim_event_inbox SET processing_state='received' WHERE processing_state='processing'"
            )
            connection.execute(
                "UPDATE eim_delivery_outbox SET state='commit_unknown' WHERE state='delivering'"
            )
        return recovered

    def cleanup_retention(
        self,
        *,
        logs_before: str,
        completed_payloads_before: str,
        failed_payloads_before: str,
    ) -> dict[str, int]:
        """按调用方计算的 UTC 边界清理日志和短期原始载荷。"""

        with self._write_lock, self._connect() as connection:
            logs = connection.execute(
                "DELETE FROM eim_logs WHERE timestamp < ?",
                (logs_before,),
            ).rowcount
            completed = connection.execute(
                """
                UPDATE eim_event_inbox SET payload_json='{}'
                WHERE processing_state='completed' AND received_at < ?
                  AND payload_json!='{}'
                  AND NOT EXISTS (
                      SELECT 1 FROM eim_delivery_outbox AS outbox
                      WHERE outbox.task_id=eim_event_inbox.task_id
                        AND outbox.event_id=eim_event_inbox.event_id
                        AND outbox.state!='completed'
                  )
                """,
                (completed_payloads_before,),
            ).rowcount
            failed = connection.execute(
                """
                UPDATE eim_event_inbox SET payload_json='{}'
                WHERE received_at < ? AND (
                    processing_state IN ('failed', 'dead_letter')
                    OR EXISTS (
                        SELECT 1 FROM eim_delivery_outbox AS outbox
                        WHERE outbox.task_id=eim_event_inbox.task_id
                          AND outbox.event_id=eim_event_inbox.event_id
                          AND outbox.state!='completed'
                    )
                  ) AND payload_json!='{}'
                """,
                (failed_payloads_before,),
            ).rowcount
            expired_metadata = connection.execute(
                """
                DELETE FROM eim_event_inbox
                WHERE received_at < ? AND payload_json='{}'
                  AND processing_state NOT IN ('received', 'processing')
                """,
                (logs_before,),
            ).rowcount
            completed_deliveries = connection.execute(
                """
                UPDATE eim_delivery_outbox SET payload_json='{}'
                WHERE state='completed' AND updated_at < ? AND payload_json!='{}'
                """,
                (completed_payloads_before,),
            ).rowcount
            expired_dead_letters = connection.execute(
                "DELETE FROM eim_delivery_outbox WHERE state='dead_letter' AND updated_at < ?",
                (failed_payloads_before,),
            ).rowcount
        return {
            "logs": logs,
            "completed_payloads": completed,
            "failed_payloads": failed,
            "expired_event_metadata": expired_metadata,
            "completed_delivery_payloads": completed_deliveries,
            "expired_dead_letters": expired_dead_letters,
        }

    def count_overview(self) -> dict[str, int]:
        with self._connect() as connection:
            task_rows = connection.execute(
                """
                SELECT observed_state, COUNT(*) AS count
                FROM eim_tasks WHERE deleted_at IS NULL GROUP BY observed_state
                """
            ).fetchall()
            today = utc_now()[:10]
            received = connection.execute(
                "SELECT COUNT(*) FROM eim_event_inbox WHERE received_at >= ?",
                (today,),
            ).fetchone()[0]
            archived = connection.execute(
                "SELECT COUNT(*) FROM eim_delivery_outbox WHERE state='completed' AND updated_at >= ?",
                (today,),
            ).fetchone()[0]
            failed = connection.execute(
                "SELECT COUNT(*) FROM eim_delivery_outbox WHERE state='dead_letter' AND updated_at >= ?",
                (today,),
            ).fetchone()[0]
        states = {str(row["observed_state"]): int(row["count"]) for row in task_rows}
        return {
            "running": states.get("running", 0),
            "stopped": states.get("stopped", 0) + states.get("stopped_app_exit", 0),
            "degraded": states.get("degraded", 0) + states.get("error", 0),
            "received_today": int(received),
            "archived_today": int(archived),
            "failed_today": int(failed),
        }

    def task_counts_today(self) -> dict[str, dict[str, int]]:
        """按任务汇总今日归档与失败数，首页不为每一行重复查询数据库。"""

        today = utc_now()[:10]
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id,
                       SUM(CASE WHEN state='completed' THEN 1 ELSE 0 END) AS archived,
                       SUM(CASE WHEN state='dead_letter' THEN 1 ELSE 0 END) AS failed
                FROM eim_delivery_outbox
                WHERE updated_at >= ?
                GROUP BY task_id
                """,
                (today,),
            ).fetchall()
        return {
            str(row["task_id"]): {
                "archived_today": int(row["archived"] or 0),
                "failed_today": int(row["failed"] or 0),
            }
            for row in rows
        }

    @staticmethod
    def _required_task(connection: sqlite3.Connection, task_id: str) -> EIMTask:
        row = connection.execute(
            "SELECT payload FROM eim_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        task = EIMRepository._task_from_row(row)
        if task is None:
            raise KeyError(f"EIM 任务不存在：{task_id}")
        return task

    def export_rows(self, table: str) -> Iterable[sqlite3.Row]:
        """仅供受控导出器读取允许表，拒绝任意 SQL 表名。"""

        allowed = {"eim_logs", "eim_audit_log"}
        if table not in allowed:
            raise ValueError("不允许导出该 EIM 表")
        with self._connect() as connection:
            return list(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
