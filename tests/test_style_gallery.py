"""样式模版画廊：目录、样例预览、导出、导入自定义样式、提案确认选用自定义样式。"""

CUSTOM_TOKENS = {
    "accent": "#7C3AED",
    "text": "#333333",
    "title": "#1F1030",
    "radius": 8,
    "header": "center",
    "section_heading": "dot",
    "quote": "soft_card",
    "end": "tip",
}


def test_gallery_lists_builtins_with_previews_and_export(client):
    styles = client.get("/api/typesetting-styles").json()["styles"]
    assert len(styles) == 8
    assert all(style["builtin"] for style in styles)
    for style in styles:
        preview = client.get(f"/api/typesetting-styles/{style['id']}/preview").json()
        assert "<section" in preview["html"]
        assert style["name"] in preview["name"] or preview["name"] == style["name"]
        # 四种内容形态的样例目录随预览返回
        assert [s["id"] for s in preview["samples"]] == ["opinion", "data", "tutorial", "listicle"]
    # 指定样例：数据速览含四张指标卡的内容，教程样例含 STEP 语义章节
    data_sample = client.get("/api/typesetting-styles/biz-navy/preview?sample=data").json()
    assert "98.5%" in data_sample["html"] and data_sample["sample"]["id"] == "data"
    tutorial_sample = client.get("/api/typesetting-styles/academic-frame/preview?sample=tutorial").json()
    assert "第一步" in tutorial_sample["html"]
    listicle_sample = client.get("/api/typesetting-styles/vivid-orange/preview?sample=listicle").json()
    assert "盘点" in listicle_sample["html"]
    dark = client.get("/api/typesetting-styles/dark-tech/preview").json()["html"]
    assert "<table" in dark and "#080808" in dark
    exported = client.get("/api/typesetting-styles/academic-frame/export").json()
    assert exported["tokens"]["accent"] == "#14532D"


def test_style_import_flow(client):
    payload = {"name": "测试紫", "vibe": "品牌", "suits": "周报", "tokens": CUSTOM_TOKENS}
    created = client.post("/api/typesetting-styles", json=payload)
    assert created.status_code == 200, created.text
    style_id = created.json()["style"]["id"]

    styles = client.get("/api/typesetting-styles").json()["styles"]
    assert len(styles) == 9
    assert any(s["id"] == style_id and not s["builtin"] for s in styles)

    preview = client.get(f"/api/typesetting-styles/{style_id}/preview").json()["html"]
    assert "#7C3AED" in preview
    assert "<script" not in preview

    assert client.post("/api/typesetting-styles", json=payload).status_code == 409
    assert (
        client.post("/api/typesetting-styles", json={"name": "非法色", "tokens": {"accent": "red"}}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/typesetting-styles", json={"name": "<script>x", "tokens": CUSTOM_TOKENS}
        ).status_code
        == 200
    )

    assert client.delete(f"/api/typesetting-styles/{style_id}").status_code == 200
    remaining = client.get("/api/typesetting-styles").json()["styles"]
    assert len(remaining) == 9  # 8 内置 + 恶意名那条（名称本身无害，令牌已校验）
    assert client.delete("/api/typesetting-styles/dark-tech").status_code == 404


def test_minimal_import_derives_palette_and_font_enum(client):
    """只填主色即可导入（其余颜色自动派生），font 支持中文枚举。"""
    created = client.post(
        "/api/typesetting-styles", json={"name": "极简导入", "tokens": {"accent": "#0F766E", "font": "songti"}}
    )
    assert created.status_code == 200, created.text
    style_id = created.json()["style"]["id"]

    preview = client.get(f"/api/typesetting-styles/{style_id}/preview?sample=tutorial").json()["html"]
    assert "准备素材" in preview, "教程样例应含竖向步骤卡"
    assert "SimSun" in preview, "songti 枚举应映射为字体栈"
    opinion = client.get(f"/api/typesetting-styles/{style_id}/preview?sample=opinion").json()["html"]
    assert "立项" in opinion and "border-left" in opinion, "观点样例应含时间线"
    data = client.get(f"/api/typesetting-styles/{style_id}/preview?sample=data").json()["html"]
    assert "方案 A" in data and "<table" in data, "数据样例应含对比表"
    assert "rgba(" not in data and "display:flex" not in data, "组件必须保持微信安全"


def test_article_render_preview_endpoint(client):
    session = client.post("/api/sessions", json={"title": "预览任务", "goal": "g"}).json()["session"]
    client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("cover.png", __import__("tests.conftest", fromlist=["png_payload"]).png_payload(), "image/png")},
    )
    article_id = client.post(
        f"/api/sessions/{session['id']}/generate", json={"instruction": "写一篇短文"}
    ).json()["article_id"]

    dark = client.get(f"/api/articles/{article_id}/render-preview?style_id=dark-tech").json()
    assert "#080808" in dark["html"] and "<table" in dark["html"]
    assert dark["style_id"] == "dark-tech"
    default = client.get(f"/api/articles/{article_id}/render-preview").json()
    assert "预览任务" in default["html"] or default["html"]
    unknown = client.get(f"/api/articles/{article_id}/render-preview?style_id=nope").json()
    assert unknown["style_id"] in {"", default["style_id"]}  # 未知样式回退
    assert "<script" not in dark["html"]


def test_custom_style_selectable_in_proposal_confirm(client):
    created = client.post(
        "/api/typesetting-styles",
        json={"name": "提案紫", "tokens": CUSTOM_TOKENS},
    ).json()
    style_id = created["style"]["id"]

    session = client.post("/api/sessions", json={"title": "自定义样式提案", "goal": "g"}).json()["session"]
    client.post(f"/api/sessions/{session['id']}/propose", json={"instruction": "写一篇文章"})
    confirmed = client.post(
        f"/api/sessions/{session['id']}/confirm-proposal", json={"style_id": style_id}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["typesetting_id"] == style_id

    generated = client.post(f"/api/sessions/{session['id']}/generate", json={"instruction": "写"})
    article_id = generated.json()["article_id"]
    article = client.get(f"/api/articles/{article_id}").json()["article"]
    assert article["typesetting_id"] == style_id

    # 自定义样式在确认后被接受，未知样式回退默认
    rejected = client.post(
        "/api/sessions", json={"title": "回退验证", "goal": "g"}
    ).json()["session"]
    client.post(f"/api/sessions/{rejected['id']}/propose", json={"instruction": "写一篇短文"})
    fallback = client.post(
        f"/api/sessions/{rejected['id']}/confirm-proposal", json={"style_id": "not-a-style"}
    )
    assert fallback.json()["typesetting_id"] == "mono-minimal"
