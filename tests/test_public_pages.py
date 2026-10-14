from pathlib import Path

from tests.test_auth import auth_client


def test_unauthenticated_root_shows_landing_overview(tmp_path):
    with auth_client(tmp_path) as client:
        response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "AI 公众号内容生产与发布平台" in html
    assert "万仞" in html
    # 旧品牌名（已按商标合规要求全面移除）不得再出现在公开页面。
    # 以 unicode 转义书写，避免源码中再次出现该词明文。
    assert "\u58a8\u821f" not in html  # legacy brand name (removed), escaped for regression check
    assert response.headers["cache-control"] == "no-cache, must-revalidate"
    assert '/assets/product-preview.png' in html
    assert 'href="/login"' in html
    assert 'href="/register"' in html
    assert 'id="login-form"' not in html


def test_login_and_register_are_standalone_pages(tmp_path):
    with auth_client(tmp_path) as client:
        login = client.get("/login")
        register = client.get("/register")
    assert login.status_code == 200
    assert register.status_code == 200
    assert 'id="login-form"' in login.text
    assert 'id="register-form"' in login.text
    assert "创建万仞账号" in register.text or "登录万仞" in register.text


def test_authenticated_users_get_workspace_and_auth_redirects(tmp_path):
    with auth_client(tmp_path) as client:
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@wanren.local", "password": "wanren-dev-admin"},
        )
    assert login.status_code == 200
    cookies = login.cookies
    root = client.get("/", cookies=cookies)
    assert root.status_code == 200
    assert 'id="workspace-view"' in root.text
    assert 'class="landing-shell"' not in root.text

    login_page = client.get("/login", cookies=cookies, follow_redirects=False)
    register_page = client.get("/register", cookies=cookies, follow_redirects=False)
    assert login_page.status_code == 303
    assert register_page.status_code == 303


def test_auth_disabled_root_remains_workbench(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="workspace-view"' in response.text


def test_landing_visual_asset_exists_and_is_raster():
    path = Path("static/assets/product-preview.png")
    assert path.is_file()
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_workspace_branding_has_no_legacy_product_character():
    workspace = Path("static/index.html").read_text(encoding="utf-8")
    assert '<div class="brand-mark">万</div>' in workspace
    auth_page = Path("static/auth.html").read_text(encoding="utf-8")
    assert '>万</span>' in auth_page  # 登录独立页品牌标记（旧登录浮层已下线）
    assert ">墨<" not in workspace
    for script_name in ("static/app.js", "static/site.js"):
        script = Path(script_name).read_text(encoding="utf-8")
        assert "墨青" not in script
        assert "石墨" not in script
