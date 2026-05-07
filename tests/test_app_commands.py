from core.app_commands import parse_app_command


def test_parse_open_whatsapp_command():
    parsed = parse_app_command("open whatsapp")

    assert parsed is not None
    assert parsed.intent == "open_app"
    assert parsed.app_name == "whatsapp"
    assert parsed.requested_app == "whatsapp"


def test_parse_open_wahtsapp_command_corrects_typo():
    parsed = parse_app_command("open wahtsapp")

    assert parsed is not None
    assert parsed.intent == "open_app"
    assert parsed.app_name == "whatsapp"
    assert parsed.normalized_text == "open whatsapp"


def test_parse_browser_tab_command_is_not_treated_as_app_command():
    assert parse_app_command("switch to second tab") is None


def test_parse_open_youtube_in_chrome_prefers_browser_routing():
    assert parse_app_command("open youtube in chrome") is None


def test_parse_search_command_is_not_treated_as_app_command():
    assert parse_app_command("search whatsapp features") is None


def test_parse_referential_close_command_defers_to_context_resolution():
    assert parse_app_command("close it") is None
    assert parse_app_command("close current app") is None


def test_parse_file_explorer_command_is_treated_as_app_command():
    parsed = parse_app_command("open file explorer")

    assert parsed is not None
    assert parsed.intent == "open_app"
    assert parsed.app_name == "explorer"


def test_parse_file_manager_command_maps_to_explorer():
    parsed = parse_app_command("open file manager")

    assert parsed is not None
    assert parsed.intent == "open_app"
    assert parsed.app_name == "explorer"


def test_parse_folder_target_defers_to_file_routing():
    assert parse_app_command("open albert folder") is None
