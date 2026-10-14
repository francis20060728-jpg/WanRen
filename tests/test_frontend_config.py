from pathlib import Path


def test_initial_page_has_six_theme_tokens_and_login_page_controls():
    html = Path("static/index.html").read_text(encoding="utf-8")
    for theme in ["mo", "azure", "graphite", "vermilion", "violet", "tide"]:
        assert f'[data-theme="{theme}"]' in html
    assert "theme-switcher" in html  # 设置页主题切换仍在工作台内
    auth_page = Path("static/auth.html").read_text(encoding="utf-8")
    for marker in ["register-email", "register-password", "login-email", "login-password"]:
        assert marker in auth_page  # 登录/注册控件在独立页（旧登录浮层已下线）
