"""
Deterministic browser-command parsing shared by intent detection and skills.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re

from core.app_launcher import canonicalize_app_name, website_url_for

_BROWSER_ALIASES = {
    "browser": "browser",
    "web browser": "browser",
    "default browser": "browser",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
    "mozilla firefox": "firefox",
    "brave": "brave",
    "brave browser": "brave",
}

_BROWSER_NAMES_PATTERN = r"(?:google chrome|chrome|default browser|web browser|browser|microsoft edge|edge|mozilla firefox|firefox|brave browser|brave)"
_BROWSER_SUFFIX_RE = re.compile(
    rf"^(?P<body>.+?)\s+(?:in|on|using)\s+(?P<browser>{_BROWSER_NAMES_PATTERN})$",
    flags=re.IGNORECASE,
)
_LAUNCH_WITH_TARGET_RE = re.compile(
    rf"^(?:open|launch|start)\s+(?P<browser>{_BROWSER_NAMES_PATTERN})\s+(?:with|to)\s+(?P<target>.+)$",
    flags=re.IGNORECASE,
)
_SWITCH_TAB_RE = re.compile(
    r"^(?:switch to|go to|open|focus)\s+(?:(?:tab\s+)?(?P<number>\d+)|(?P<ordinal>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\s+tab)$",
    flags=re.IGNORECASE,
)

_SEARCH_PREFIXES = ("search for ", "search ", "google ", "find ", "look up ", "lookup ")
_LAUNCH_COMMANDS = {
    "open chrome",
    "launch chrome",
    "start chrome",
    "open browser",
    "launch browser",
    "start browser",
    "open google chrome",
}
_CLOSE_COMMANDS = {
    "close chrome",
    "quit chrome",
    "exit chrome",
    "close browser",
    "quit browser",
    "exit browser",
    "close current chrome window",
    "close chrome window",
}
_NEW_TAB_COMMANDS = {"new tab", "open tab", "open new tab"}
_CLOSE_TAB_COMMANDS = {"close tab", "close this tab", "close current tab"}
_NEXT_TAB_COMMANDS = {"next tab", "go to next tab", "switch to next tab"}
_PREVIOUS_TAB_COMMANDS = {"previous tab", "prev tab", "go to previous tab", "switch to previous tab"}
_BACK_COMMANDS = {"back", "go back"}
_FORWARD_COMMANDS = {"forward", "go forward"}
_REFRESH_COMMANDS = {"refresh", "refresh page", "reload", "reload page"}
_HOME_COMMANDS = {"home", "go home", "open home page"}
_SCROLL_DOWN_COMMANDS = {"scroll down", "page down"}
_SCROLL_UP_COMMANDS = {"scroll up", "page up"}
_READ_TITLE_COMMANDS = {"read page title", "read title", "what is the page title", "page title"}
_READ_PAGE_COMMANDS = {
    "read page",
    "summarize page",
    "summarise page",
    "what is on page",
    "what's on page",
    "read results",
}
_COPY_URL_COMMANDS = {"copy page url", "copy url", "copy current url", "copy page link", "copy link"}
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
}
_LIKELY_WEB_TLDS = {"ai", "app", "co", "com", "dev", "gg", "in", "io", "me", "net", "org", "tv"}


@dataclass(frozen=True, slots=True)
class ParsedBrowserCommand:
    action: str
    browser: str = ""
    explicit_browser: bool = False
    query: str = ""
    website: str = ""
    url: str = ""
    tab_index: int = 0


def parse_browser_command(text: str) -> ParsedBrowserCommand | None:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return None

    launch_match = _LAUNCH_WITH_TARGET_RE.match(normalized)
    if launch_match:
        browser = normalize_browser_name(launch_match.group("browser"))
        target = launch_match.group("target").strip()
        return _target_to_command(target, browser=browser, explicit_browser=True)

    stripped, browser, explicit_browser = strip_explicit_browser_reference(normalized)

    if normalized in _LAUNCH_COMMANDS or stripped in _LAUNCH_COMMANDS:
        return ParsedBrowserCommand(action="launch_browser", browser=browser or "chrome", explicit_browser=explicit_browser)

    if normalized in _CLOSE_COMMANDS or stripped in _CLOSE_COMMANDS:
        return ParsedBrowserCommand(action="close_browser", browser=browser or "chrome", explicit_browser=explicit_browser)

    if stripped in _NEW_TAB_COMMANDS:
        return ParsedBrowserCommand(action="new_tab", browser=browser, explicit_browser=explicit_browser)
    if stripped in _CLOSE_TAB_COMMANDS:
        return ParsedBrowserCommand(action="close_tab", browser=browser, explicit_browser=explicit_browser)
    if stripped in _NEXT_TAB_COMMANDS:
        return ParsedBrowserCommand(action="next_tab", browser=browser, explicit_browser=explicit_browser)
    if stripped in _PREVIOUS_TAB_COMMANDS:
        return ParsedBrowserCommand(action="previous_tab", browser=browser, explicit_browser=explicit_browser)

    switch_match = _SWITCH_TAB_RE.match(stripped)
    if switch_match:
        tab_index = _parse_tab_index(switch_match.group("number"), switch_match.group("ordinal"))
        if tab_index > 0:
            return ParsedBrowserCommand(
                action="switch_tab",
                browser=browser,
                explicit_browser=explicit_browser,
                tab_index=tab_index,
            )

    if stripped in _BACK_COMMANDS:
        return ParsedBrowserCommand(action="go_back", browser=browser, explicit_browser=explicit_browser)
    if stripped in _FORWARD_COMMANDS:
        return ParsedBrowserCommand(action="go_forward", browser=browser, explicit_browser=explicit_browser)
    if stripped in _REFRESH_COMMANDS:
        return ParsedBrowserCommand(action="refresh", browser=browser, explicit_browser=explicit_browser)
    if stripped in _HOME_COMMANDS:
        return ParsedBrowserCommand(action="home", browser=browser, explicit_browser=explicit_browser)
    if stripped in _SCROLL_DOWN_COMMANDS or stripped.startswith("scroll down"):
        return ParsedBrowserCommand(action="scroll_down", browser=browser, explicit_browser=explicit_browser)
    if stripped in _SCROLL_UP_COMMANDS or stripped.startswith("scroll up"):
        return ParsedBrowserCommand(action="scroll_up", browser=browser, explicit_browser=explicit_browser)
    if stripped in _READ_TITLE_COMMANDS:
        return ParsedBrowserCommand(action="read_page_title", browser=browser, explicit_browser=explicit_browser)
    if stripped in _READ_PAGE_COMMANDS:
        return ParsedBrowserCommand(action="read_page", browser=browser, explicit_browser=explicit_browser)
    if stripped in _COPY_URL_COMMANDS:
        return ParsedBrowserCommand(action="copy_page_url", browser=browser, explicit_browser=explicit_browser)

    for prefix in _SEARCH_PREFIXES:
        if stripped.startswith(prefix):
            query = stripped[len(prefix) :].strip()
            if query:
                return ParsedBrowserCommand(
                    action="search",
                    browser=browser,
                    explicit_browser=explicit_browser,
                    query=query,
                )

    if stripped.startswith("open "):
        target = stripped[5:].strip()
        if target:
            return _target_to_command(target, browser=browser, explicit_browser=explicit_browser)

    return None


def strip_explicit_browser_reference(text: str) -> tuple[str, str, bool]:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return "", "", False

    match = _BROWSER_SUFFIX_RE.match(normalized)
    if not match:
        return normalized, "", False

    browser = normalize_browser_name(match.group("browser"))
    cleaned = " ".join(match.group("body").split()).strip()
    return cleaned, browser, bool(browser)


def normalize_browser_name(value: str) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    if not normalized:
        return ""
    canonical = canonicalize_app_name(normalized)
    if canonical in {"chrome", "edge", "firefox", "brave"}:
        return canonical
    return _BROWSER_ALIASES.get(normalized, "")


def _target_to_command(target: str, *, browser: str = "", explicit_browser: bool = False) -> ParsedBrowserCommand:
    normalized_target = " ".join(str(target or "").strip().split())
    if not normalized_target:
        return ParsedBrowserCommand(action="launch_browser", browser=browser, explicit_browser=explicit_browser)

    url = website_url_for(normalized_target)
    if url:
        return ParsedBrowserCommand(
            action="open_url",
            browser=browser,
            explicit_browser=explicit_browser,
            website=normalized_target,
            url=url,
        )

    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(?:/.*)?", normalized_target, flags=re.IGNORECASE):
        suffix = normalized_target.rsplit(".", 1)[-1].split("/", 1)[0].lower()
        if suffix not in _LIKELY_WEB_TLDS and not re.match(r"^[a-z]+://", normalized_target, flags=re.IGNORECASE):
            return ParsedBrowserCommand(
                action="search",
                browser=browser,
                explicit_browser=explicit_browser,
                query=normalized_target,
            )
        url = normalized_target if re.match(r"^[a-z]+://", normalized_target, flags=re.IGNORECASE) else f"https://{normalized_target}"
        return ParsedBrowserCommand(
            action="open_url",
            browser=browser,
            explicit_browser=explicit_browser,
            website=normalized_target,
            url=url,
        )

    return ParsedBrowserCommand(
        action="search",
        browser=browser,
        explicit_browser=explicit_browser,
        query=normalized_target,
    )


def _parse_tab_index(number: str | None, ordinal: str | None) -> int:
    if number:
        try:
            return max(1, min(int(number), 9))
        except ValueError:
            return 0
    if ordinal:
        return _ORDINALS.get(ordinal.lower(), 0)
    return 0
