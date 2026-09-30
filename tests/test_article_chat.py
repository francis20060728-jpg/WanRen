import json


def test_article_chat_stream_creates_ai_version(client):
    session = client.post(
        "/api/sessions",
        json={
            "title": "对话优化任务",
            "goal": "生成一篇可继续对话修改的文章",
            "audience": "公众号读者",
            "tone": "专业、清晰、克制",
        },
    ).json()["session"]
    generated = client.post(
        f"/api/sessions/{session['id']}/generate",
        json={"instruction": "生成一篇结构清晰的文章"},
    ).json()
    article_id = generated["article_id"]

    response = client.post(
        f"/api/articles/{article_id}/chat",
        json={"instruction": "请精简全文，并把标题改为“手机阅读短读”"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [
        line[7:].strip()
        for line in response.text.splitlines()
        if line.startswith("event: ")
    ]
    assert "user" in events
    assert "delta" in events
    assert "completed" in events

    detail = client.get(f"/api/articles/{article_id}").json()
    assert detail["article"]["title"] == "手机阅读短读"
    assert len(detail["versions"]) == 2
    assert detail["versions"][-1]["source"] == "AI"
    # V4 起生成与改稿都进入进度对话：初稿公告 + 本次改稿各占一组 USER/ASSISTANT
    assert [message["role"] for message in detail["messages"]] == [
        "USER",
        "ASSISTANT",
        "USER",
        "ASSISTANT",
    ]
    metadata = json.loads(detail["messages"][-1]["metadata_json"])
    assert metadata["version"] == 2
    assert metadata["review"]["decision"] in {"PASS", "REVISE", "REJECT"}


def test_article_chat_accepts_unquoted_natural_title_instruction(client):
    session = client.post(
        "/api/sessions",
        json={"title": "自然标题修订任务", "goal": "验证无引号标题指令"},
    ).json()["session"]
    generated = client.post(
        f"/api/sessions/{session['id']}/generate",
        json={"instruction": "生成一篇短文"},
    ).json()
    response = client.post(
        f"/api/articles/{generated['article_id']}/chat",
        json={"instruction": "请保留关键信息，并把标题改为自然标题修订后的版本"},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/articles/{generated['article_id']}").json()
    assert detail["article"]["title"] == "自然标题修订后的版本"
