"""
Deterministic input normalization and app-aware correction.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Any

from core.app_index import AppMatch, get_app_matcher
from core.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - optional fast path
    from rapidfuzz import fuzz, process as rf_process

    _RAPIDFUZZ_OK = True
except Exception:  # pragma: no cover
    fuzz = None
    rf_process = None
    _RAPIDFUZZ_OK = False


@dataclass(frozen=True, slots=True)
class TokenCorrection:
    original: str
    corrected: str
    reason: str
    confidence: float


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    original_text: str
    normalized_text: str
    tokens: tuple[str, ...]
    corrected_tokens: tuple[str, ...]
    corrections: tuple[TokenCorrection, ...] = ()
    matched_app: str = ""
    confidence: float = 0.0
    requires_confirmation: bool = False
    confirmation_prompt: str = ""

    @property
    def corrected(self) -> bool:
        return self.normalized_text != self.original_text.strip().lower()


_LEADING_FILLERS = (
    "please ",
    "pls ",
    "plz ",
    "kindly ",
    "could you ",
    "can you ",
    "would you ",
    "can u ",
    "could u ",
)
_SAFE_FILLERS = {"please", "pls", "plz", "just", "kindly"}
_TRAILING_POLITE_TOKENS = {"please", "now", "thanks"}
_VERB_CANONICALIZATION: tuple[tuple[str, str], ...] = (
    (r"^(?:launch|start|run)\b", "open"),
    (r"^(?:quit|exit|terminate|kill)\b", "close"),
    (r"^(?:switch to|bring to front|bring up|activate)\b", "focus"),
    (r"^(?:hide)\b", "minimize"),
    (r"^(?:enlarge|expand)\b", "maximize"),
    (r"^(?:look up|lookup|google)\b", "search"),
    # Good morning variations
    (r"^(?:good\s+morning|good\s+mornin|good\s+mornng)\b", "good morning"),
    (r"^(?:good\s+evening|good\s+afternoon)\b", "good day"),
)
_PHRASE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("current tab", "tab"),
    ("this tab", "tab"),
    # Word misplacements - reorder common patterns
    ("app open", "open app"),
    ("app close", "close app"),
    ("music play", "play music"),
    ("video play", "play video"),
    ("song play", "play song"),
    ("spotify open", "open spotify"),
    ("whatsapp open", "open whatsapp"),
    ("youtube open", "open youtube"),
    ("chrome open", "open chrome"),
    ("telegram open", "open telegram"),
    # More misplacement patterns
    ("close chrome", "close chrome"),  # Keep as-is but ensures works
    ("open app", "open app"),  # Already correct
    # App + action misplacements
    ("window close", "close window"),
    ("window minimize", "minimize window"),
    ("window maximize", "maximize window"),
    ("tab close", "close tab"),
    ("tab open", "open tab"),
    # Media misplacements
    ("pause music", "pause music"),
    ("play music", "play music"),
    ("next song", "next song"),
    ("previous song", "previous song"),
    # Handle "the" in different positions
    ("open the app", "open app"),
    ("close the app", "close app"),
    ("minimize the app", "minimize app"),
    ("maximize the app", "maximize app"),
    # Communication app misplacements (critical - handle "say X to Y on app")
    ("say message", "send message"),
    ("say hi", "send hi"),
    ("say to", "tell"),
    ("tell hi", "tell"),
    # Telegram command patterns
    ("send telegram", "telegram"),
    ("message on telegram", "telegram"),
    # Good morning variations
    ("good morining", "good morning"),
    ("good mornin", "good morning"),
    ("good mornng", "good morning"),
    ("morning", "good morning"),
    # More word order issues
    ("play dulander", "dulander play"),
    ("play movie", "play movie"),
    ("play songs", "play songs"),
    ("open telegram", "open telegram"),
    # Additional common misplacements
    ("close the telegram", "close telegram"),
    ("type message", "send message"),
    # Contact + platform patterns
    ("on whatsapp", "whatsapp"),
    ("on telegram", "telegram"),
    ("via whatsapp", "whatsapp"),
    ("via telegram", "telegram"),
)
_TOKEN_CORRECTIONS = {
    # Open command typos
    "opn": "open", "oen": "open", "opan": "open", "opun": "open",
    "oepn": "open", "epn": "open", "pen": "open", "poen": "open",
    # Close command typos
    "clos": "close", "clsoe": "close", "clsoe": "close", "colse": "close",
    "cose": "close", "coes": "close", "cloes": "close",
    # Search command typos
    "serch": "search", "seach": "search", "sarch": "search",
    "serch": "search", "sarch": "search", "serach": "search",
    # Play command typos
    "plae": "play", "pley": "play", "ply": "play", "paly": "play",
    "plau": "play", "pla": "play",
    # Volume typos
    "volum": "volume", "vol": "volume", "voulme": "volume", "volme": "volume",
    # Brightness typos
    "brighness": "brightness", "brightnes": "brightness", "brightnedd": "brightness",
    "britness": "brightness", "brigness": "brightness", "brigthness": "brightness",
    # Mute/unmute typos
    "mut": "mute", "untmute": "unmute", "unmte": "unmute", "umute": "unmute",
    # Communication/command typos
    "sy": "say", "sya": "say", "sayy": "say", "sa": "say",
    "tel": "tell", "telle": "tell", "telt": "tell",
    "snd": "send", "sned": "send", "sen": "send",
    "cal": "call", "calle": "call", "calll": "call",
    "msge": "message", "mssage": "message",
    # Minimize/maximize
    "minimise": "minimize", "maximise": "maximize", "minmise": "minimize",
    "maxmise": "maximize", "minimize": "minimize", "maximize": "maximize",
    # Message typos
    "massage": "message", "masssage": "message", "mesage": "message",
    "messge": "message", "messsage": "message", "mesage": "message",
    "msg": "message", "mssg": "message",
    # Name corrections - contact names
    "hemanth": "hemanth", "hemant": "hemanth", "hemnath": "hemanth",
    "hemath": "hemanth", "hemanthh": "hemanth",
    # App name corrections
    "whatssap": "whatsapp", "watsapp": "whatsapp", "watsap": "whatsapp",
    "whatsap": "whatsapp", "whatspp": "whatsapp", "wahtsapp": "whatsapp",
    "yotube": "youtube", "youtub": "youtube", "yutube": "youtube", "utube": "youtube",
    "yt": "youtube", "ytube": "youtube", "youtubE": "youtube",
    "spoify": "spotify", "spofiy": "spotify", "spotfy": "spotify", "spotiy": "spotify",
    "crome": "chrome", "chome": "chrome", "crom": "chrome", "chorm": "chrome",
    "microdoft": "microsoft", "mircosoft": "microsoft", "microsft": "microsoft",
    "slack": "slack", "slak": "slack", "slakc": "slack",
    "vscode": "vs code", "vscod": "vs code", "vsc": "vs code",
    "code": "vs code",  # Context-dependent, handled specially
    "discord": "discord", "disord": "discord", "discrod": "discord", "discor": "discord",
    "telegram": "telegram", "telagram": "telegram", "telegran": "telegram",
    "telgram": "telegram", "telegrm": "telegram",
    "teams": "teams", "team": "teams", "tems": "teams",
    "zoom": "zoom", "zoon": "zoom", "zom": "zoom",
    "notion": "notion", "notoin": "notion", "notiion": "notion",
    "figma": "figma", "figam": "figma", "figma": "figma",
    # Common STT confusions
    "teh": "the", "hte": "the", "th": "the",
    "adn": "and", "dna": "and", "nad": "and",
    "taht": "that", "thta": "that",
    "fo": "for", "fro": "for", "form": "for",
    "yuo": "you", "u": "you", "yu": "you", "yo": "you",
    "ca": "can", "cna": "can", "cn": "can",
    "whre": "where", "wer": "where", "hwre": "where",
    "whn": "when", "wn": "when", "hn": "when",
    # App aliases
    "insta": "instagram", "ig": "instagram", "instram": "instagram",
    # Additional common STT typos
    "dulandar": "dulander", "dulandeer": "dulander", "dulandear": "dulander",
    "dulandar": "dulander",
    # Media related
    "youtubemusic": "youtube music", "ytmusic": "youtube music",
    # System-related
    "shutdow": "shutdown", "shutdon": "shutdown", "shutdow": "shutdown",
    "restrt": "restart", "restart": "restart",
    # New greeting variations for better detection
    "hlo": "hello", "hola": "hello", "helloo": "hello",
    "hai": "hi", "hii": "hi", "hy": "hi",
    "heya": "hey", "heyy": "hey",
}
_KNOWN_VERBS = (
    "back",
    "click",
    "create",
    "close",
    "delete",
    "find",
    "focus",
    "help",
    "lock",
    "maximize",
    "message",
    "minimize",
    "move",
    "mute",
    "next",
    "open",
    "pause",
    "play",
    "previous",
    "rename",
    "restart",
    "restore",
    "search",
    "select",
    "send",
    "set",
    "shutdown",
    "tap",
    "toggle",
    "unmute",
    "volume",
    # Common typos that should be corrected
    "massage",  # Will be corrected to "message" via fuzzy matching
)
_PROTECTED_TOKENS = {
    "what",
    "who",
    "where",
    "why",
    "when",
    "how",
    "can",
    "could",
    "would",
    "should",
    "you",
    "your",
    "do",
    "does",
    "did",
    "is",
    "are",
    "the",
    "a",
    "an",
    "it",
    "this",
    "that",
    "my",
    "me",
    "file",
    "files",
    "folder",
    "folders",
    "document",
    "documents",
    "tab",
    "tabs",
    "up",
    "down",
    "left",
    "right",
    "first",
    "second",
    "third",
    "one",
    "two",
    "three",
    "page",
    "playback",
    "today",
    "tomorrow",
    "yesterday",
}
_ALLOWED_PUNCTUATION = {".", "/", "\\", ":", "_", "-", "%", "'", '"'}
_QUOTE_PATTERN = re.compile(r'"[^"]*"|\'[^\']*\'')
_COMMAND_VERBS = {"open", "close", "minimize", "maximize", "focus", "toggle", "restore", "play"}
_COMMAND_STOP_TOKENS = {"and", "then", "after", "followed", "for", "to"}
_BROWSER_PREPOSITIONS = {"in", "on", "using", "with"}
_BROWSER_TARGETS = ("chrome", "edge")
_FILE_LEAD_TOKENS = {"file", "folder", "directory", "document", "documents"}
_APP_STYLE_FILE_PREFIXES = {
    ("file", "explorer"),
    ("windows", "explorer"),
    ("file", "manager"),
}
_FUZZY_CACHE: dict[str, str] = {}


def normalize_input(raw_text: str, settings: Any = None, context: Any = None) -> str:
    del settings, context
    return normalize_command(raw_text)


def normalize_command(text: str) -> str:
    return normalize_command_result(text).normalized_text


@lru_cache(maxsize=2048)
def normalize_command_result(text: str) -> NormalizationResult:
    return _normalize_impl(text)


def _normalize_impl(raw_text: str) -> NormalizationResult:
    source = str(raw_text or "").strip()
    if not source:
        return NormalizationResult("", "", (), ())

    quoted_segments: dict[str, str] = {}

    def _store_quote(match: re.Match[str]) -> str:
        key = f"__QUOTE_{len(quoted_segments)}__"
        quoted_segments[key] = match.group(0).strip()
        return f" {key} "

    working = source.replace("\u2019", "'").replace("\u2018", "'")
    working = working.replace("\u201c", '"').replace("\u201d", '"')
    working = _QUOTE_PATTERN.sub(_store_quote, working)
    working = working.lower().strip()
    working = _normalize_punctuation(working)
    working = re.sub(r"\s+", " ", working).strip()

    for filler in _LEADING_FILLERS:
        if working.startswith(filler):
            working = working[len(filler) :].strip()
            break

    working = _strip_safe_fillers(working)

    for source_phrase, target_phrase in _PHRASE_REPLACEMENTS:
        working = re.sub(rf"\b{re.escape(source_phrase)}\b", target_phrase, working)

    for pattern, replacement in _VERB_CANONICALIZATION:
        working = re.sub(pattern, replacement, working)

    raw_tokens = tuple(token for token in working.split() if token)
    corrected_tokens = list(_fuzzy_correct_tokens(list(raw_tokens)))
    corrections: list[TokenCorrection] = []

    for index, token in enumerate(raw_tokens):
        if index < len(corrected_tokens) and corrected_tokens[index] != token:
            corrections.append(
                TokenCorrection(token, corrected_tokens[index], "token_normalization", 0.99)
            )

    corrected_tokens, app_match, app_corrections, confirmation_prompt = _apply_app_corrections(corrected_tokens)
    corrections.extend(app_corrections)

    normalized_text = " ".join(corrected_tokens)
    normalized_text = re.sub(r"\b(open|close|minimize|maximize|focus|toggle|restore)\s+the\s+", r"\1 ", normalized_text)
    normalized_text = re.sub(r"\s+", " ", normalized_text).strip()

    for key, quoted in quoted_segments.items():
        normalized_text = normalized_text.replace(key.lower(), quoted)

    result = NormalizationResult(
        original_text=source,
        normalized_text=normalized_text,
        tokens=raw_tokens,
        corrected_tokens=tuple(corrected_tokens),
        corrections=tuple(corrections),
        matched_app=app_match.canonical_name if app_match is not None else "",
        confidence=app_match.confidence if app_match is not None else 0.0,
        requires_confirmation=bool(app_match and app_match.requires_confirmation and not app_match.should_autocorrect),
        confirmation_prompt=confirmation_prompt,
    )

    if result.corrections or result.requires_confirmation:
        logger.info(
            "[Correction]",
            input=source,
            tokens=list(result.tokens),
            corrected_tokens=list(result.corrected_tokens),
            matched_app=result.matched_app,
            confidence=round(result.confidence, 3),
            requires_confirmation=result.requires_confirmation,
        )

    return result


def _normalize_punctuation(text: str) -> str:
    characters: list[str] = []
    for char in text:
        if char.isalnum() or char.isspace() or char in _ALLOWED_PUNCTUATION:
            characters.append(char)
        else:
            characters.append(" ")
    return "".join(characters)


def _strip_safe_fillers(text: str) -> str:
    tokens = text.split()
    while len(tokens) > 1 and tokens and tokens[0] in _SAFE_FILLERS:
        tokens.pop(0)
    return " ".join(tokens)


def _fuzzy_correct_tokens(tokens: list[str]) -> list[str]:
    if not _RAPIDFUZZ_OK or rf_process is None or fuzz is None:
        return [_TOKEN_CORRECTIONS.get(token, token) for token in tokens]

    corrected = [_TOKEN_CORRECTIONS.get(token, token) for token in tokens]
    for index, token in enumerate(corrected[:4]):
        if len(token) < 3 or token in _KNOWN_VERBS or token in _PROTECTED_TOKENS:
            continue
        if _looks_structured(token):
            continue
        cached = _FUZZY_CACHE.get(token)
        if cached is not None:
            corrected[index] = cached
            continue
        match = rf_process.extractOne(token, _KNOWN_VERBS, scorer=fuzz.WRatio)
        if match is None:
            continue
        candidate, score, _ = match
        if score >= 90:
            corrected[index] = candidate
            _FUZZY_CACHE[token] = candidate
    return corrected


def _apply_app_corrections(tokens: list[str]) -> tuple[list[str], AppMatch | None, list[TokenCorrection], str]:
    if not tokens:
        return tokens, None, [], ""

    corrected = list(tokens)
    corrections: list[TokenCorrection] = []
    best_match: AppMatch | None = None
    confirmation_prompt = ""

    corrected, browser_match, browser_corrections, browser_prompt = _correct_browser_qualifier(corrected)
    corrections.extend(browser_corrections)
    if browser_match is not None:
        best_match = browser_match
        confirmation_prompt = browser_prompt
        if browser_match.requires_confirmation:
            return corrected, browser_match, corrections, browser_prompt

    if corrected[0] not in _COMMAND_VERBS:
        return corrected, best_match, corrections, confirmation_prompt

    verb = corrected[0]
    target_start = 1
    while target_start < len(corrected) and corrected[target_start] in {"the", "my"}:
        target_start += 1
    if target_start >= len(corrected):
        return corrected, best_match, corrections, confirmation_prompt
    if corrected[target_start] in _FILE_LEAD_TOKENS:
        prefix = tuple(corrected[target_start : target_start + 2])
        if prefix not in _APP_STYLE_FILE_PREFIXES:
            return corrected, best_match, corrections, confirmation_prompt

    target_end = len(corrected)
    for index in range(target_start, len(corrected)):
        token = corrected[index]
        if token in _COMMAND_STOP_TOKENS:
            target_end = index
            break
        if token in _BROWSER_PREPOSITIONS and index > target_start:
            target_end = index
            break

    match_end = target_end
    while match_end > target_start and corrected[match_end - 1] in _TRAILING_POLITE_TOKENS:
        match_end -= 1
    if match_end <= target_start:
        match_end = target_end

    allowed_targets = ()
    include_web = verb in {"open", "play"}
    if verb == "play":
        allowed_targets = ("spotify", "youtube")

    span_match = _match_prefix_span(corrected[target_start:match_end], allowed_targets=allowed_targets, include_web=include_web)
    if span_match is None:
        return corrected, best_match, corrections, confirmation_prompt

    matched_phrase = " ".join(corrected[target_start : target_start + span_match[1]])
    app_match = span_match[0]
    replacement_tokens = app_match.canonical_name.split()

    if app_match.should_autocorrect:
        corrected = (
            corrected[:target_start]
            + replacement_tokens
            + corrected[target_start + span_match[1] :]
        )
        corrections.append(
            TokenCorrection(matched_phrase, app_match.canonical_name, f"app_{app_match.layer}", app_match.confidence)
        )
        return corrected, app_match, corrections, confirmation_prompt

    if app_match.requires_confirmation:
        prompt = f"Did you mean {app_match.canonical_name}?"
        return corrected, app_match, corrections, prompt

    return corrected, best_match, corrections, confirmation_prompt


def _correct_browser_qualifier(tokens: list[str]) -> tuple[list[str], AppMatch | None, list[TokenCorrection], str]:
    for index in range(len(tokens) - 1):
        if tokens[index] not in _BROWSER_PREPOSITIONS:
            continue
        suffix = tokens[index + 1 :]
        if not suffix:
            continue
        best = _match_prefix_span(suffix[:2], allowed_targets=_BROWSER_TARGETS, include_web=False)
        if best is None:
            continue
        app_match, span_len = best
        phrase = " ".join(tokens[index + 1 : index + 1 + span_len])
        if app_match.should_autocorrect:
            corrected = (
                tokens[: index + 1]
                + app_match.canonical_name.split()
                + tokens[index + 1 + span_len :]
            )
            return corrected, app_match, [TokenCorrection(phrase, app_match.canonical_name, "browser_qualifier", app_match.confidence)], ""
        if app_match.requires_confirmation:
            return tokens, app_match, [], f"Did you mean {app_match.canonical_name}?"
    return tokens, None, [], ""


def _match_prefix_span(
    tokens: list[str],
    *,
    allowed_targets: tuple[str, ...] = (),
    include_web: bool = True,
) -> tuple[AppMatch, int] | None:
    matcher = get_app_matcher()
    best: tuple[AppMatch, int] | None = None
    max_span = min(3, len(tokens))
    for span_len in range(max_span, 0, -1):
        phrase = " ".join(tokens[:span_len]).strip()
        if not phrase or _looks_path_like(phrase):
            continue
        match = matcher.match(phrase, allowed_targets=allowed_targets, include_web=include_web)
        if match is None:
            continue
        if best is None or match.confidence > best[0].confidence:
            best = (match, span_len)
        if match.confidence >= 0.99:
            return best
    return best


def _looks_structured(token: str) -> bool:
    return any(marker in token for marker in (".", "\\", "/", ":"))


def _looks_path_like(text: str) -> bool:
    return any(marker in text for marker in (".", "\\", "/", ":")) or text.startswith('"')
