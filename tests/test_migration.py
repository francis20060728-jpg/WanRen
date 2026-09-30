"""升级数据安全：旧版数据库经 _migrate_schema 后，既有数据完好且新列/新表就绪。

升级迭代不得丢数据是公开承诺（README「停止 / 升级 / 备份」）。迁移只做
加列 / 建表 / 回填，绝不改写或删除既有行。
"""
import sqlite3

from wanren.database import Database


OLD_SCHEMA = """
CREATE TABLE workspace (id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE official_account (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspace(id),
    name TEXT NOT NULL, app_id TEXT NOT NULL, token_status TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE content_session (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspace(id),
    official_account_id TEXT NOT NULL REFERENCES official_account(id),
    title TEXT NOT NULL, status TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '', tone TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '[]', brief TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE session_asset (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL, file_name TEXT NOT NULL, mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, storage_key TEXT NOT NULL, processing_status TEXT NOT NULL,
    extracted_text TEXT NOT NULL DEFAULT '', structured_data TEXT NOT NULL DEFAULT '{}',
    vision_result TEXT NOT NULL DEFAULT '{}', metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE article (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    title TEXT NOT NULL, summary TEXT NOT NULL, cover_asset_id TEXT,
    content_json TEXT NOT NULL, status TEXT NOT NULL, current_version_id TEXT,
    original_declaration_status TEXT, original_declaration_version_id TEXT,
    original_declaration_confirmed_by TEXT, original_declaration_confirmed_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE article_version (
    id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL, source TEXT NOT NULL, content_json TEXT NOT NULL,
    change_summary TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE publish_job (
    id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    official_account_id TEXT NOT NULL REFERENCES official_account(id), mode TEXT NOT NULL,
    status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, external_id TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE ai_run (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES content_session(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def _build_old_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO workspace VALUES ('ws_old','老空间','ACTIVE','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO official_account VALUES ('oa_old','ws_old','老账号','wxold','READY','{}','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO content_session VALUES ('sess_old','ws_old','oa_old','旧对话','DONE','','','','[]','{}',"
        "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO session_asset VALUES ('asset_old','sess_old','PDF','old-paper.pdf','application/pdf',"
        "1024,'sess_old/asset_old.pdf','READY','旧论文正文','{}','{}','{}',"
        "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO article VALUES ('art_old','sess_old','旧标题','旧摘要',NULL,"
        "'{\"title\":\"旧标题\"}','PUBLISHED',NULL,NULL,NULL,NULL,NULL,"
        "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO article_version VALUES ('ver_old','art_old',1,'AI','{\"title\":\"旧标题\"}','初版',"
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO publish_job VALUES ('job_old','art_old','oa_old','publish','DONE','key-old','wxmock',"
        "0,'{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO ai_run VALUES ('run_old','sess_old','generate','builtin','local-editorial-v2',"
        "100,200,3000,'SUCCESS','2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_migration_preserves_old_data_and_adds_new_columns(tmp_path):
    db_path = tmp_path / "wanren.sqlite3"
    _build_old_db(db_path)

    db = Database(db_path)  # 启动即迁移
    try:
        # 旧数据一行不少
        article = db.query_one("SELECT * FROM article WHERE id='art_old'")
        assert article["title"] == "旧标题"
        assert article["summary"] == "旧摘要"
        asset = db.query_one("SELECT * FROM session_asset WHERE id='asset_old'")
        assert asset["extracted_text"] == "旧论文正文"
        assert db.query_one("SELECT * FROM article_version WHERE id='ver_old'")["version_no"] == 1
        assert db.query_one("SELECT * FROM publish_job WHERE id='job_old'")["status"] == "DONE"
        assert db.query_one("SELECT * FROM ai_run WHERE id='run_old'")["input_tokens"] == 100

        # 新能力就绪：新列存在、新表存在
        article_columns = {row["name"] for row in db.execute("PRAGMA table_info(article)").fetchall()}
        assert "typesetting_id" in article_columns
        version_columns = {row["name"] for row in db.execute("PRAGMA table_info(article_version)").fetchall()}
        assert "highlight_json" in version_columns
        publish_columns = {row["name"] for row in db.execute("PRAGMA table_info(publish_job)").fetchall()}
        assert {"scheduled_at", "article_url"} <= publish_columns
        asset_columns = {row["name"] for row in db.execute("PRAGMA table_info(session_asset)").fetchall()}
        assert "article_id" in asset_columns
        assert db.query_one("SELECT COUNT(*) AS n FROM typesetting_style")["n"] == 0

        # 历史回填：会话唯一文章的公共素材归属到该文章
        assert asset["article_id"] == "art_old"
    finally:
        db.close()

    # 再次启动（幂等）：数据仍完好
    db2 = Database(db_path)
    try:
        assert db2.query_one("SELECT * FROM article WHERE id='art_old'")["title"] == "旧标题"
    finally:
        db2.close()
