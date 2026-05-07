from core.apps_registry import canonicalize_app_name


def test_apps_registry_canonicalize_supports_browser_alias_flag():
    assert canonicalize_app_name("browser", resolve_browser_alias=False) == "browser"
    assert canonicalize_app_name("google chrome", resolve_browser_alias=False) == "chrome"
