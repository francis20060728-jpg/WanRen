import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi.testclient import TestClient

from wanren.app import create_app
from wanren.database import Database


TOKEN = "callback-token"
ACCOUNT_ID = "oa_default"


def callback_client(tmp_path, token=TOKEN):
    env = {
        "WANREN_DB_PATH": str(tmp_path / "callback.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "callback-storage"),
        "WANREN_PUBLISHER": "mock",
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "dev",
    }
    if token is not None:
        env["WANREN_WECHAT_CALLBACK_TOKEN"] = token
    return TestClient(create_app(env))


def signature(timestamp, nonce, token=TOKEN):
    return hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode()).hexdigest()


def params(timestamp="1786812000", nonce="nonce-001", token=TOKEN):
    return {
        "signature": signature(timestamp, nonce, token),
        "timestamp": timestamp,
        "nonce": nonce,
    }


def login(client):
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@wanren.local", "password": "wanren-dev-admin"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def create_publishing_job(client):
    headers = login(client)
    session = client.post(
        "/api/sessions", headers=headers, json={"title": "微信回调任务"}
    ).json()["session"]
    article_id = client.post(
        f"/api/sessions/{session['id']}/generate", headers=headers, json={}
    ).json()["article_id"]
    client.post(
        f"/api/articles/{article_id}/approve", headers=headers, json={"reviewer": "tester"}
    )
    job = client.post(
        f"/api/articles/{article_id}/publish", headers=headers, json={"mode": "publish"}
    ).json()
    service = client.app.state.service
    with service.db.tx():
        service.db.execute(
            "UPDATE publish_job SET status='PUBLISHING',external_id=? WHERE id=?",
            ("publish-10001", job["id"]),
        )
        service.db.execute(
            "UPDATE article SET status='PUBLISHING' WHERE id=?", (article_id,)
        )
    return job["id"], article_id


def event_xml(
    publish_status="0",
    publish_id="publish-10001",
    article_url="https://mp.weixin.qq.com/s/final",
    msg_id=None,
):
    msg_id_element = f"<MsgId><![CDATA[{msg_id}]]></MsgId>" if msg_id is not None else ""
    return f"""
    <xml>
      <ToUserName><![CDATA[gh_test]]></ToUserName>
      <FromUserName><![CDATA[oUser]]></FromUserName>
      <CreateTime>1786812000</CreateTime>
      <MsgType><![CDATA[event]]></MsgType>
      {msg_id_element}
      <Event><![CDATA[PUBLISHJOBFINISH]]></Event>
      <publish_id><![CDATA[{publish_id}]]></publish_id>
      <PublishEventInfo>
        <publish_status><![CDATA[{publish_status}]]></publish_status>
        <article_id><![CDATA[article_external_id]]></article_id>
        <article_detail>
          <count>1</count>
          <item>
            <idx>1</idx>
            <article_url><![CDATA[{article_url}]]></article_url>
          </item>
        </article_detail>
      </PublishEventInfo>
    </xml>
    """.encode()


def test_callback_url_verification(tmp_path):
    with callback_client(tmp_path) as client:
        response = client.get(
            f"/wechat/{ACCOUNT_ID}/callback",
            params={**params(), "echostr": "echo-12345"},
        )
        assert response.status_code == 200
        assert response.text == "echo-12345"


def test_callback_url_verification_rejects_invalid_signature(tmp_path):
    with callback_client(tmp_path) as client:
        response = client.get(
            f"/wechat/{ACCOUNT_ID}/callback",
            params={**params(), "signature": "bad", "echostr": "echo"},
        )
        assert response.status_code == 403
        assert "Invalid" in response.json()["detail"]


def test_callback_requires_configured_token(tmp_path):
    with callback_client(tmp_path, token=None) as client:
        response = client.get(
            f"/wechat/{ACCOUNT_ID}/callback",
            params={"signature": "x", "timestamp": "1", "nonce": "n", "echostr": "e"},
        )
        assert response.status_code == 503


def test_callback_unknown_account(tmp_path):
    with callback_client(tmp_path) as client:
        response = client.get(
            "/wechat/missing/callback",
            params={**params(), "echostr": "e"},
        )
        assert response.status_code == 404


def test_successful_publish_callback_updates_job_article_and_audit(tmp_path):
    with callback_client(tmp_path) as client:
        job_id, article_id = create_publishing_job(client)
        response = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(publish_status="0"),
            headers={"Content-Type": "text/xml"},
        )
        assert response.status_code == 200
        assert response.text == "success"

        service = client.app.state.service
        job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
        article = service.db.query_one("SELECT * FROM article WHERE id=?", (article_id,))
        assert job["status"] == "PUBLISHED"
        assert job["article_url"] == "https://mp.weixin.qq.com/s/final"
        assert json.loads(job["last_error"])["publish_id"] == "publish-10001"
        assert article["status"] == "PUBLISHED"
        audit = service.db.query_one(
            "SELECT * FROM audit_log WHERE action='publish.callback' AND resource_id=?",
            (job_id,),
        )
        assert audit is not None


def test_duplicate_publish_callback_is_idempotent(tmp_path):
    with callback_client(tmp_path) as client:
        job_id, _ = create_publishing_job(client)
        first = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(),
        )
        second = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(nonce="retry-nonce"),
            content=event_xml(),
        )
        assert first.status_code == 200
        assert second.status_code == 200

        service = client.app.state.service
        events = service.db.query_all(
            "SELECT * FROM wechat_callback_event WHERE publish_id=?", ("publish-10001",)
        )
        audits = service.db.query_all(
            "SELECT * FROM audit_log WHERE action='publish.callback' AND resource_id=?",
            (job_id,),
        )
        assert len(events) == 1
        assert events[0]["status"] == "PROCESSED"
        assert events[0]["attempt_count"] == 1
        assert events[0]["duplicate_count"] == 1
        assert len(audits) == 1


def test_canonical_event_payload_deduplicates_across_xml_whitespace(tmp_path):
    with callback_client(tmp_path) as client:
        create_publishing_job(client)
        first = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(),
        )
        reformatted = event_xml().replace(b"<xml>", b"<xml>\n      ")
        second = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(nonce="retry-reformatted"),
            content=reformatted,
        )
        assert first.status_code == 200
        assert second.status_code == 200

        service = client.app.state.service
        event = service.db.query_one(
            "SELECT * FROM wechat_callback_event WHERE publish_id=?", ("publish-10001",)
        )
        assert event is not None
        assert event["duplicate_count"] == 1


def test_changed_publish_status_is_processed_as_a_new_event(tmp_path):
    with callback_client(tmp_path) as client:
        job_id, _ = create_publishing_job(client)
        publishing = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(publish_status="1"),
        )
        published = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(nonce="final-status"),
            content=event_xml(publish_status="0"),
        )
        assert publishing.status_code == 200
        assert published.status_code == 200

        service = client.app.state.service
        events = service.db.query_all(
            "SELECT * FROM wechat_callback_event WHERE publish_id=?", ("publish-10001",)
        )
        audits = service.db.query_all(
            "SELECT * FROM audit_log WHERE action='publish.callback' AND resource_id=?",
            (job_id,),
        )
        job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
        assert len(events) == 2
        assert len(audits) == 2
        assert job["status"] == "PUBLISHED"


def test_duplicate_callback_while_processing_returns_conflict(tmp_path):
    with callback_client(tmp_path) as client:
        job_id, _ = create_publishing_job(client)
        service = client.app.state.service
        body = event_xml()
        root = ET.fromstring(body)
        event_info = {
            "event": "PUBLISHJOBFINISH",
            "publish_id": "publish-10001",
            "publish_status": "0",
            "article_url": "https://mp.weixin.qq.com/s/final",
            "fail_idx": [],
        }
        event_key = service._wechat_callback_event_key(ACCOUNT_ID, root, event_info)
        service.db.claim_wechat_callback_event(
            event_key=event_key,
            official_account_id=ACCOUNT_ID,
            event="PUBLISHJOBFINISH",
            publish_id="publish-10001",
            payload_json=json.dumps(event_info),
        )

        response = client.post(
            f"/wechat/{ACCOUNT_ID}/callback", params=params(), content=body
        )
        assert response.status_code == 409
        job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
        event = service.db.query_one(
            "SELECT * FROM wechat_callback_event WHERE event_key=?", (event_key,)
        )
        assert job["status"] == "PUBLISHING"
        assert event["status"] == "PROCESSING"
        assert event["duplicate_count"] == 1


def test_failed_callback_event_can_be_retried(tmp_path):
    with callback_client(tmp_path) as client:
        job_id, _ = create_publishing_job(client)
        service = client.app.state.service
        body = event_xml()
        root = ET.fromstring(body)
        event_info = {
            "event": "PUBLISHJOBFINISH",
            "publish_id": "publish-10001",
            "publish_status": "0",
            "article_url": "https://mp.weixin.qq.com/s/final",
            "fail_idx": [],
        }
        event_key = service._wechat_callback_event_key(ACCOUNT_ID, root, event_info)
        service.db.claim_wechat_callback_event(
            event_key=event_key,
            official_account_id=ACCOUNT_ID,
            event="PUBLISHJOBFINISH",
            publish_id="publish-10001",
            payload_json=json.dumps(event_info),
        )
        with service.db.tx():
            service.db.execute(
                "UPDATE wechat_callback_event SET status='FAILED' WHERE event_key=?",
                (event_key,),
            )

        response = client.post(
            f"/wechat/{ACCOUNT_ID}/callback", params=params(), content=body
        )
        assert response.status_code == 200
        event = service.db.query_one(
            "SELECT * FROM wechat_callback_event WHERE event_key=?", (event_key,)
        )
        job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
        assert event["status"] == "PROCESSED"
        assert event["attempt_count"] == 2
        assert job["status"] == "PUBLISHED"


def test_unknown_publish_callback_is_deduplicated(tmp_path):
    with callback_client(tmp_path) as client:
        first = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(publish_id="unknown"),
        )
        second = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(nonce="unknown-retry"),
            content=event_xml(publish_id="unknown"),
        )
        assert first.status_code == 200
        assert second.status_code == 200

        service = client.app.state.service
        event = service.db.query_one(
            "SELECT * FROM wechat_callback_event WHERE publish_id=?", ("unknown",)
        )
        assert event is not None
        assert event["status"] == "PROCESSED"
        assert event["duplicate_count"] == 1


def test_wechat_message_id_takes_precedence_for_event_identity(tmp_path):
    with callback_client(tmp_path) as client:
        create_publishing_job(client)
        first = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(msg_id="wechat-message-001"),
        )
        second = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(nonce="same-message"),
            content=event_xml(publish_status="1", msg_id="wechat-message-001"),
        )
        assert first.status_code == 200
        assert second.status_code == 200

        service = client.app.state.service
        event = service.db.query_one(
            "SELECT * FROM wechat_callback_event WHERE publish_id=?", ("publish-10001",)
        )
        assert event is not None
        assert event["duplicate_count"] == 1
        job = service.db.query_one("SELECT * FROM publish_job LIMIT 1")
        assert job["status"] == "PUBLISHED"


def test_legacy_database_gets_callback_event_table(tmp_path):
    path = tmp_path / "legacy-callback.sqlite3"
    original = Database(path)
    original.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE wechat_callback_event")

    migrated = Database(path)
    try:
        columns = {
            row["name"] for row in migrated.query_all("PRAGMA table_info(wechat_callback_event)")
        }
        indexes = {
            row["name"] for row in migrated.query_all("PRAGMA index_list(wechat_callback_event)")
        }
        assert {
            "event_key", "official_account_id", "publish_id", "status",
            "attempt_count", "duplicate_count", "result_json", "last_error",
        }.issubset(columns)
        assert "idx_wechat_callback_status" in indexes
    finally:
        migrated.close()
    assert Path(path).is_file()


def test_callback_maps_every_wechat_publish_status(tmp_path):
    mapping = {
        "0": "PUBLISHED",
        "1": "PUBLISHING",
        "2": "FAILED",
        "3": "FAILED",
        "4": "FAILED",
        "5": "FAILED",
    }
    for index, (wechat_status, expected) in enumerate(mapping.items()):
        with callback_client(tmp_path / f"status-{wechat_status}") as client:
            job_id, article_id = create_publishing_job(client)
            response = client.post(
                f"/wechat/{ACCOUNT_ID}/callback",
                params=params(nonce=f"status-{index}"),
                content=event_xml(publish_status=wechat_status, publish_id="publish-10001"),
            )
            assert response.status_code == 200
            service = client.app.state.service
            job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
            article = service.db.query_one("SELECT * FROM article WHERE id=?", (article_id,))
            assert job["status"] == expected
            assert article["status"] == expected


def test_callback_rejects_unsupported_status_missing_id_malformed_and_dtd(tmp_path):
    with callback_client(tmp_path) as client:
        create_publishing_job(client)
        unsupported = client.post(
            f"/wechat/{ACCOUNT_ID}/callback", params=params(), content=event_xml(publish_status="9")
        )
        assert unsupported.status_code == 400

        missing_id = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(publish_id=""),
        )
        assert missing_id.status_code == 400

        malformed = client.post(
            f"/wechat/{ACCOUNT_ID}/callback", params=params(), content=b"<xml><broken>"
        )
        assert malformed.status_code == 400

        dtd = b"""<!DOCTYPE xml [<!ENTITY x SYSTEM "file:///windows/win.ini">]><xml>&x;</xml>"""
        blocked = client.post(
            f"/wechat/{ACCOUNT_ID}/callback", params=params(), content=dtd
        )
        assert blocked.status_code == 400
        assert "forbidden" in blocked.json()["detail"]


def test_callback_ignores_other_events_and_unknown_publish_ids(tmp_path):
    with callback_client(tmp_path) as client:
        ignored = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=b"<xml><Event><![CDATA[SUBSCRIBE]]></Event></xml>",
        )
        assert ignored.status_code == 200

        unknown = client.post(
            f"/wechat/{ACCOUNT_ID}/callback",
            params=params(),
            content=event_xml(publish_id="unknown"),
        )
        assert unknown.status_code == 200
