from datetime import datetime

from core.query_parser import is_probable_file_search, parse_natural_query


def test_parse_resume_pdf_query():
    query = parse_natural_query("Find my resume PDF", now=datetime(2026, 4, 23, 9, 0, 0))

    assert query.intent_action == "find"
    assert query.keywords == ["resume"]
    assert query.file_types == ["pdf"]
    assert query.sort_by == "relevance"


def test_parse_latest_downloaded_image_query():
    query = parse_natural_query("Open last downloaded image", now=datetime(2026, 4, 23, 9, 0, 0))

    assert query.intent_action == "open"
    assert query.file_types == ["image"]
    assert query.folders == ["downloads"]
    assert query.sort_by == "latest"


def test_parse_modified_yesterday_query():
    query = parse_natural_query("Find report modified yesterday", now=datetime(2026, 4, 23, 9, 0, 0))

    assert query.keywords == ["report"]
    assert query.date_from == datetime(2026, 4, 22, 0, 0, 0)
    assert query.date_to == datetime(2026, 4, 23, 0, 0, 0)


def test_file_search_heuristic_rejects_plain_web_search():
    assert is_probable_file_search("search IPL score") is False


def test_file_search_heuristic_rejects_playback_command():
    assert is_probable_file_search("resume the video") is False
