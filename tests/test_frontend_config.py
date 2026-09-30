from pathlib import Path


def test_initial_page_has_six_theme_tokens_and_registration_controls():
    html = Path("static/index.html").read_text(encoding="utf-8")
    for theme in ["mo", "azure", "graphite", "vermilion", "violet", "tide"]:
        assert f'[data-theme="{theme}"]' in html
    for marker in [
        "auth-login-panel",
        "auth-register-panel",
        "register-email",
        "register-password",
        "register-confirm",
        "auth-theme-switcher",
        "theme-switcher",
    ]:
        assert marker in html
