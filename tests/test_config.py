import pytest

from wanren.config import load_settings


def test_production_auth_requires_admin_password():
    with pytest.raises(ValueError, match="WANREN_ADMIN_PASSWORD"):
        load_settings({"WANREN_AUTH_MODE": "production"})


def test_dev_auth_uses_explicit_local_default():
    settings = load_settings({"WANREN_AUTH_MODE": "dev"})
    assert settings.admin_password == "wanren-dev-admin"


def test_search_configuration_defaults_and_bounds():
    settings = load_settings({})
    assert settings.search_provider == "duckduckgo"
    assert settings.search_timeout_seconds == 15
    assert settings.search_max_results == 5

    with pytest.raises(ValueError, match="WANREN_SEARCH_PROVIDER"):
        load_settings({"WANREN_SEARCH_PROVIDER": "unknown"})
    with pytest.raises(ValueError, match="Search timeout"):
        load_settings({"WANREN_SEARCH_TIMEOUT_SECONDS": "0"})
    with pytest.raises(ValueError, match="result count"):
        load_settings({"WANREN_SEARCH_MAX_RESULTS": "21"})
