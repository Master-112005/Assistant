import pytest

from core import state
from core.intent import IntentDetector, IntentType


@pytest.fixture
def detector():
    return IntentDetector()


@pytest.fixture(autouse=True)
def reset_intent_state():
    state.current_context = "unknown"
    state.current_app = "unknown"
    state.current_window_title = ""
    state.current_process_name = ""
    state.youtube_active = False
    state.music_active = False
    state.active_music_provider = ""
    state.last_media_action = ""
    state.last_successful_action = ""
    yield


def test_open_app(detector):
    res = detector.detect("open chrome", "open chrome")
    assert res.intent == IntentType.OPEN_APP
    assert res.entities.get("app") == "chrome"
    assert res.entities.get("app_name") == "chrome"
    assert res.confidence > 0.8


def test_open_whatsapp_prefers_app_routing(detector):
    res = detector.detect("open whatsapp", "open whatsapp")
    assert res.intent == IntentType.OPEN_APP
    assert res.entities.get("app") == "whatsapp"


def test_open_wahtsapp_typo_normalizes_to_whatsapp_app(detector):
    res = detector.detect("open wahtsapp", "open wahtsapp")
    assert res.intent == IntentType.OPEN_APP
    assert res.entities.get("app") == "whatsapp"


def test_close_app(detector):
    res = detector.detect("close chrome", "close chrome")
    assert res.intent == IntentType.CLOSE_APP
    assert res.entities.get("app") == "chrome"


def test_search_web(detector):
    res = detector.detect("search for IPL score", "search for IPL score")
    assert res.intent == IntentType.SEARCH_WEB
    assert res.entities.get("query") == "ipl score"


def test_search_web_in_chrome_extracts_browser(detector):
    res = detector.detect("search for IPL score in chrome", "search for IPL score in chrome")
    assert res.intent == IntentType.SEARCH_WEB
    assert res.entities.get("query") == "ipl score"
    assert res.entities.get("browser") == "chrome"


def test_open_website_in_chrome_extracts_browser(detector):
    res = detector.detect("open youtube in chrome", "open youtube in chrome")
    assert res.intent == IntentType.OPEN_WEBSITE
    assert res.entities.get("browser") == "chrome"
    assert res.entities.get("url") == "https://www.youtube.com"


def test_browser_new_tab_command(detector):
    res = detector.detect("open new tab", "open new tab")
    assert res.intent == IntentType.BROWSER_TAB_NEW


def test_browser_switch_tab_command(detector):
    res = detector.detect("switch to second tab", "switch to second tab")
    assert res.intent == IntentType.BROWSER_TAB_SWITCH
    assert res.entities.get("tab_index") == 2


def test_browser_refresh_command(detector):
    res = detector.detect("refresh page", "refresh page")
    assert res.intent == IntentType.BROWSER_ACTION
    assert res.entities.get("action") == "refresh"


def test_question(detector):
    res = detector.detect("what time is it", "what time is it")
    assert res.intent == IntentType.QUESTION


def test_volume_up(detector):
    res = detector.detect("increase volume", "increase volume")
    assert res.intent == IntentType.VOLUME_UP
    assert res.entities.get("control") == "volume"
    assert res.entities.get("direction") == "up"


def test_set_volume_value(detector):
    res = detector.detect("set volume to 50", "set volume to 50")
    assert res.intent == IntentType.SET_VOLUME
    assert res.entities.get("action") == "set_volume"
    assert res.entities.get("value") == 50


def test_reduce_the_volume_to_value_routes_to_set_volume(detector):
    res = detector.detect("reduce the volume to 50", "reduce the volume to 50")

    assert res.intent == IntentType.SET_VOLUME
    assert res.entities.get("value") == 50


def test_brightness_typo_with_target_value_routes_to_set_brightness(detector):
    res = detector.detect("increase the brightnedd to 50", "increase the brightnedd to 50")

    assert res.intent == IntentType.SET_BRIGHTNESS
    assert res.entities.get("value") == 50


def test_lock_pc(detector):
    res = detector.detect("lock computer", "lock computer")
    assert res.intent == IntentType.LOCK_PC
    assert res.entities.get("action") == "lock_pc"


def test_delete_file(detector):
    res = detector.detect("delete report.pdf", "delete report.pdf")
    assert res.intent == IntentType.DELETE_FILE
    assert res.entities.get("action") == "delete"
    assert res.entities.get("filename") == "report.pdf"


def test_open_file(detector):
    res = detector.detect("open file notes.txt", "open file notes.txt")
    assert res.intent == IntentType.OPEN_FILE
    assert res.entities.get("action") == "open"
    assert res.entities.get("filename") == "notes.txt"


def test_open_named_folder_routes_to_open_file(detector):
    res = detector.detect("open albert folder", "open albert folder")

    assert res.intent == IntentType.OPEN_FILE
    assert res.entities.get("action") == "open"
    assert res.entities.get("filename") == "albert folder"


def test_open_file_explorer_routes_to_open_app(detector):
    res = detector.detect("open file explorer", "open file explorer")

    assert res.intent == IntentType.OPEN_APP


def test_resume_the_video_routes_to_media(detector):
    res = detector.detect("resume the video", "resume the video")
    assert res.intent == IntentType.PLAY_MEDIA
    assert res.confidence >= 0.7
    assert res.candidate_scores.get("media_resume", 0.0) >= res.candidate_scores.get("file_search", 0.0)


def test_pause_the_video_routes_to_media(detector):
    res = detector.detect("pause the video", "pause the video")
    assert res.intent == IntentType.PAUSE_MEDIA


def test_resume_song_routes_to_media(detector):
    res = detector.detect("resume song", "resume song")
    assert res.intent == IntentType.PLAY_MEDIA


def test_find_resume_pdf_routes_to_file_search(detector):
    res = detector.detect("find my resume pdf", "find my resume pdf")
    assert res.intent == IntentType.SEARCH_FILE
    assert res.candidate_scores.get("file_search", 0.0) > res.candidate_scores.get("media_resume", 0.0)


def test_open_resume_document_routes_to_file_open(detector):
    res = detector.detect("open resume document", "open resume document")
    assert res.intent == IntentType.OPEN_FILE


def test_bare_resume_uses_context_when_media_is_active(detector):
    res = detector.detect(
        "resume",
        "resume",
        context={
            "current_app": "youtube",
            "current_window_title": "Lofi mix - YouTube - Google Chrome",
            "current_process_name": "chrome.exe",
            "youtube_active": True,
        },
    )
    assert res.intent == IntentType.PLAY_MEDIA


def test_bare_resume_without_context_requests_clarification(detector):
    res = detector.detect("resume", "resume")
    assert res.intent == IntentType.UNKNOWN
    assert res.requires_confirmation is True
    assert res.entities.get("clarification_prompt")


def test_next_one_uses_music_context(detector):
    res = detector.detect(
        "next one",
        "next one",
        context={
            "current_app": "spotify",
            "current_window_title": "Spotify Premium",
            "current_process_name": "spotify.exe",
            "music_active": True,
            "active_music_provider": "spotify",
        },
    )
    assert res.intent == IntentType.NEXT_TRACK


def test_open_budget_xlsx_uses_explorer_context(detector):
    res = detector.detect(
        "open budget.xlsx",
        "open budget.xlsx",
        context={
            "current_app": "explorer",
            "current_window_title": "File Explorer",
            "current_process_name": "explorer.exe",
        },
    )
    assert res.intent == IntentType.OPEN_FILE


def test_rename_file(detector):
    res = detector.detect("rename report.txt to final_report.txt", "rename report.txt to final_report.txt")
    assert res.intent == IntentType.RENAME_FILE
    assert res.entities.get("action") == "rename"
    assert res.entities.get("new_name") == "final_report.txt"


def test_greeting(detector):
    res = detector.detect("hello nova", "hello nova")
    assert res.intent == IntentType.GREETING


def test_repeated_hi_variant_routes_to_greeting(detector):
    res = detector.detect("hihi", "hihi")

    assert res.intent == IntentType.GREETING


def test_greeting_plus_status_question_routes_to_question(detector):
    res = detector.detect("hi how are you", "hi how are you")

    assert res.intent == IntentType.QUESTION


def test_set_timer_routes_to_reminder_create(detector):
    res = detector.detect("set timer for 5 min", "set timer for 5 min")

    assert res.intent == IntentType.REMINDER_CREATE


def test_create_text_file_on_desktop_routes_to_create_file(detector):
    res = detector.detect("create text file on desktop", "create text file on desktop")

    assert res.intent == IntentType.CREATE_FILE
    assert res.entities.get("filename") == "New Text Document.txt"
    assert res.entities.get("location") == "desktop"


def test_help(detector):
    res = detector.detect("what can you do", "what can you do")
    assert res.intent == IntentType.HELP


def test_multi_action(detector):
    res = detector.detect("open chrome and search IPL score", "open chrome and search IPL score")
    assert res.intent == IntentType.MULTI_ACTION
    assert len(res.entities.get("segments", [])) == 2


def test_repeat_last_command(detector):
    res = detector.detect("do the same thing", "do the same thing")
    assert res.intent == IntentType.REPEAT_LAST_COMMAND


def test_unknown(detector):
    res = detector.detect("asdfgh", "asdfgh")
    assert res.intent == IntentType.UNKNOWN
    assert res.confidence == 0.0
