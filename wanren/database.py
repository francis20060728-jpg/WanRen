from __future__ import annotations

import json
import sqlite3
import threading
from functools import wraps
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _serialized(method):
    """Serialize access to the shared sqlite connection across threadpool threads."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workspace (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_user (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('ADMIN','EDITOR','REVIEWER','PUBLISHER','VIEWER')),
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED')),
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, email)
);

CREATE TABLE IF NOT EXISTS auth_session (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS official_account (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    name TEXT NOT NULL,
    app_id TEXT NOT NULL,
    token_status TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, app_id)
);

CREATE TABLE IF NOT EXISTS article_template (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    official_account_id TEXT REFERENCES official_account(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK(source IN ('BUILTIN','USER','AI_DESCRIPTION','AI_REFERENCE')),
    structure_json TEXT NOT NULL,
    style_json TEXT NOT NULL DEFAULT '{}',
    references_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED')),
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL DEFAULT 'local-operator',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS article_template_version (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES article_template(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    official_account_id TEXT REFERENCES official_account(id),
    source TEXT NOT NULL,
    structure_json TEXT NOT NULL,
    style_json TEXT NOT NULL DEFAULT '{}',
    references_json TEXT NOT NULL DEFAULT '[]',
    change_summary TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(template_id, version)
);

CREATE TABLE IF NOT EXISTS content_session (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    official_account_id TEXT NOT NULL REFERENCES official_account(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '[]',
    brief TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_asset (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    storage_key TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    extracted_text TEXT NOT NULL DEFAULT '',
    structured_data TEXT NOT NULL DEFAULT '{}',
    vision_result TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    cover_asset_id TEXT REFERENCES session_asset(id),
    content_json TEXT NOT NULL,
    typesetting_id TEXT,
    status TEXT NOT NULL,
    current_version_id TEXT,
    original_declaration_status TEXT,
    original_declaration_version_id TEXT,
    original_declaration_confirmed_by TEXT,
    original_declaration_confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_version (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    source TEXT NOT NULL,
    content_json TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(article_id, version_no)
);

CREATE TABLE IF NOT EXISTS review (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    review_type TEXT NOT NULL,
    score INTEGER NOT NULL,
    decision TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    result_json TEXT NOT NULL,
    reviewer TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_message (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_run (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    workflow_type TEXT NOT NULL,
    status TEXT NOT NULL,
    current_node TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_node_run (
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    input_ref TEXT NOT NULL DEFAULT '{}',
    output_ref TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_queue (
    workflow_run_id TEXT PRIMARY KEY REFERENCES workflow_run(id) ON DELETE CASCADE,
    workflow_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','DONE','FAILED')),
    payload TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_by TEXT,
    locked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_run (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    cost REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_job (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    official_account_id TEXT NOT NULL REFERENCES official_account(id),
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    external_id TEXT,
    article_url TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '{}',
    scheduled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wechat_callback_event (
    event_key TEXT PRIMARY KEY,
    official_account_id TEXT NOT NULL REFERENCES official_account(id),
    event TEXT NOT NULL,
    publish_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL,
    processed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS typesetting_style (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    name TEXT NOT NULL,
    vibe TEXT NOT NULL DEFAULT '',
    suits TEXT NOT NULL DEFAULT '',
    tokens_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS writing_style (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    name TEXT NOT NULL,
    vibe TEXT NOT NULL DEFAULT '',
    suits TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_setting (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_status ON content_session(status);
CREATE INDEX IF NOT EXISTS idx_asset_session ON session_asset(session_id);
CREATE INDEX IF NOT EXISTS idx_article_session ON article(session_id);
CREATE INDEX IF NOT EXISTS idx_workflow_session ON workflow_run(session_id);
CREATE INDEX IF NOT EXISTS idx_workflow_queue_ready ON workflow_queue(status,available_at);
CREATE INDEX IF NOT EXISTS idx_publish_article ON publish_job(article_id);
CREATE INDEX IF NOT EXISTS idx_wechat_callback_status ON wechat_callback_event(status,updated_at);
CREATE INDEX IF NOT EXISTS idx_template_account ON article_template(official_account_id,status);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self._migrate_schema()

    def get_setting(self, key: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT value_json FROM app_setting WHERE key = ?", (key,))
        if row is None or not row["value_json"]:
            return None
        try:
            value = json.loads(row["value_json"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def set_setting(self, key: str, value: dict[str, Any]) -> None:
        with self.tx():
            self.connection.execute(
                "INSERT INTO app_setting(key,value_json,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, tuple(params))

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.connection.close()

    @_serialized
    def claim_next_workflow(self, worker_id: str) -> dict[str, Any] | None:
        """Atomically claim one queued workflow for a worker."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT * FROM workflow_queue
                WHERE status = 'QUEUED' AND available_at <= ?
                ORDER BY created_at
                LIMIT 1
                """,
                (utc_now(),),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            item = dict(row)
            now = utc_now()
            self.connection.execute(
                """
                UPDATE workflow_queue
                SET status='RUNNING', attempts=attempts+1, locked_by=?, locked_at=?, updated_at=?
                WHERE workflow_run_id=? AND status='QUEUED'
                """,
                (worker_id, now, now, item["workflow_run_id"]),
            )
            claimed = self.connection.execute(
                "SELECT changes() AS value"
            ).fetchone()[0]
            if not claimed:
                self.connection.rollback()
                return None
            self.connection.commit()
            return item
        except Exception:
            self.connection.rollback()
            raise

    @_serialized
    def claim_due_publish_jobs(self, limit: int = 10) -> list[str]:
        """Atomically transition due scheduled publish jobs to PENDING."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                """
                SELECT p.id
                FROM publish_job p
                JOIN article a ON a.id = p.article_id
                WHERE p.status = 'SCHEDULED'
                  AND p.scheduled_at IS NOT NULL
                  AND p.scheduled_at <= ?
                  AND a.status = 'HUMAN_APPROVED'
                ORDER BY p.scheduled_at
                LIMIT ?
                """,
                (utc_now(), max(1, limit)),
            ).fetchall()
            ids = [row["id"] for row in rows]
            now = utc_now()
            for job_id in ids:
                self.connection.execute(
                    "UPDATE publish_job SET status='PENDING',updated_at=? WHERE id=? AND status='SCHEDULED'",
                    (now, job_id),
                )
            self.connection.commit()
            return ids
        except Exception:
            self.connection.rollback()
            raise

    @_serialized
    def claim_wechat_callback_event(
        self,
        *,
        event_key: str,
        official_account_id: str,
        event: str,
        publish_id: str,
        payload_json: str,
    ) -> dict[str, Any]:
        """Atomically reserve a callback event or report an existing delivery."""
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO wechat_callback_event
                (event_key,official_account_id,event,publish_id,payload_json,status,
                 attempt_count,duplicate_count,result_json,last_error,received_at,updated_at)
                VALUES (?,?,?,?,?,'PROCESSING',1,0,'{}','{}',?,?)
                """,
                (
                    event_key,
                    official_account_id,
                    event,
                    publish_id,
                    payload_json,
                    now,
                    now,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                row = self.connection.execute(
                    "SELECT * FROM wechat_callback_event WHERE event_key = ?", (event_key,)
                ).fetchone()
                self.connection.commit()
                return {**dict(row), "duplicate": False}

            row = self.connection.execute(
                "SELECT * FROM wechat_callback_event WHERE event_key = ?", (event_key,)
            ).fetchone()
            if row is None:
                self.connection.rollback()
                raise RuntimeError("WeChat callback event reservation failed")
            current = dict(row)
            if current["status"] in {"PROCESSING", "PROCESSED"}:
                self.connection.execute(
                    """
                    UPDATE wechat_callback_event
                    SET duplicate_count=duplicate_count+1,updated_at=?
                    WHERE event_key=?
                    """,
                    (now, event_key),
                )
                current["duplicate_count"] += 1
                self.connection.commit()
                return {**current, "duplicate": True}

            self.connection.execute(
                """
                UPDATE wechat_callback_event
                SET status='PROCESSING',attempt_count=attempt_count+1,updated_at=?
                WHERE event_key=?
                """,
                (now, event_key),
            )
            row = self.connection.execute(
                "SELECT * FROM wechat_callback_event WHERE event_key = ?", (event_key,)
            ).fetchone()
            self.connection.commit()
            return {**dict(row), "duplicate": False}
        except Exception:
            self.connection.rollback()
            raise

    def finish_wechat_callback_event(
        self,
        event_key: str,
        *,
        status: str,
        result_json: str = "{}",
        last_error: str = "{}",
    ) -> None:
        processed_at = utc_now() if status == "PROCESSED" else None
        with self.tx():
            self.connection.execute(
                """
                UPDATE wechat_callback_event
                SET status=?,result_json=?,last_error=?,processed_at=COALESCE(processed_at,?),updated_at=?
                WHERE event_key=?
                """,
                (status, result_json, last_error, processed_at, utc_now(), event_key),
            )

    def _migrate_schema(self) -> None:
        article_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(article)").fetchall()
        }
        if article_columns:
            if "original_declaration_status" not in article_columns:
                self.connection.execute(
                    "ALTER TABLE article ADD COLUMN original_declaration_status TEXT"
                )
            if "original_declaration_version_id" not in article_columns:
                self.connection.execute(
                    "ALTER TABLE article ADD COLUMN original_declaration_version_id TEXT"
                )
            if "original_declaration_confirmed_by" not in article_columns:
                self.connection.execute(
                    "ALTER TABLE article ADD COLUMN original_declaration_confirmed_by TEXT"
                )
            if "original_declaration_confirmed_at" not in article_columns:
                self.connection.execute(
                    "ALTER TABLE article ADD COLUMN original_declaration_confirmed_at TEXT"
                )
        template_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(article_template)").fetchall()
        }
        if template_columns and "references_json" not in template_columns:
            self.connection.execute(
                "ALTER TABLE article_template ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'"
            )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(publish_job)").fetchall()}
        if columns and "scheduled_at" not in columns:
            self.connection.execute("ALTER TABLE publish_job ADD COLUMN scheduled_at TEXT")
        if columns and "article_url" not in columns:
            self.connection.execute("ALTER TABLE publish_job ADD COLUMN article_url TEXT")
            self.connection.commit()
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_publish_schedule ON publish_job(status,scheduled_at)"
        )
        ai_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(ai_run)").fetchall()
        }
        if ai_columns and "cost" not in ai_columns:
            self.connection.execute("ALTER TABLE ai_run ADD COLUMN cost REAL NOT NULL DEFAULT 0")
        # 版本级 AI 改动高亮：新增/修改的段落文本清单（前端渲染黄色高亮）
        version_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(article_version)").fetchall()
        }
        if version_columns and "highlight_json" not in version_columns:
            self.connection.execute("ALTER TABLE article_version ADD COLUMN highlight_json TEXT NOT NULL DEFAULT '[]'")
        # 素材对话专属归属（NULL=会话公共；从文章对话上传时记录 article_id）
        asset_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(session_asset)").fetchall()
        }
        if asset_columns and "article_id" not in asset_columns:
            self.connection.execute("ALTER TABLE session_asset ADD COLUMN article_id TEXT")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_asset_article ON session_asset(article_id)"
        )
        # 历史公共素材回填：会话只有一篇文章时，归属到它（生成前上传的素材即为此文章准备）
        self.connection.execute(
            """
            UPDATE session_asset SET article_id = (
                SELECT a.id FROM article a WHERE a.session_id = session_asset.session_id LIMIT 1
            )
            WHERE article_id IS NULL
              AND (SELECT COUNT(*) FROM article a2 WHERE a2.session_id = session_asset.session_id) = 1
            """
        )
        article_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(article)").fetchall()
        }
        if article_columns and "typesetting_id" not in article_columns:
            self.connection.execute("ALTER TABLE article ADD COLUMN typesetting_id TEXT")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS typesetting_style (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspace(id),
                name TEXT NOT NULL,
                vibe TEXT NOT NULL DEFAULT '',
                suits TEXT NOT NULL DEFAULT '',
                tokens_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(workspace_id, name)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS writing_style (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspace(id),
                name TEXT NOT NULL,
                vibe TEXT NOT NULL DEFAULT '',
                suits TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(workspace_id, name)
            )
            """
        )
        session_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(content_session)").fetchall()
        }
        if session_columns and "writing_style_id" not in session_columns:
            self.connection.execute("ALTER TABLE content_session ADD COLUMN writing_style_id TEXT")
        self.connection.commit()


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:20]}"
