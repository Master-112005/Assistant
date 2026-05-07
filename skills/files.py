"""
Natural-language file-management skill.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from core import entities, settings, state
from core.file_index import FileIndex
from core.file_search import SearchResult, SmartFileSearch
from core.files import FileActionResult, FileManager
from core.logger import get_logger
from core.query_parser import is_probable_file_search
from core.safety import FileSafetyPolicy
from core.text_utils import normalize_command, extract_ordinal as _shared_extract_ordinal
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)

_YES_WORDS = {"yes", "y", "confirm", "continue", "proceed", "do it", "okay", "ok"}
_NO_WORDS = {"no", "n", "cancel", "stop", "never mind", "dont", "don't"}
_ORDINALS = {
    "first": 1,
    "1": 1,
    "1st": 1,
    "one": 1,
    "second": 2,
    "2": 2,
    "2nd": 2,
    "two": 2,
    "third": 3,
    "3": 3,
    "3rd": 3,
    "three": 3,
    "fourth": 4,
    "4": 4,
    "4th": 4,
    "four": 4,
    "fifth": 5,
    "5": 5,
    "5th": 5,
    "five": 5,
}
_MEDIA_COMMAND_PATTERN = re.compile(
    r"^(?:(?:play|pause|resume|skip|next|previous|shuffle|repeat|continue|stop|mute|unmute)\b.*|search\s+(?:song|track|music)\b)\b(song|music|track|album|artist|playlist|liked songs|video|playback|player|movie|youtube|spotify)?",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ParsedFileCommand:
    action: str
    reference: str = ""
    destination: str = ""
    new_name: str = ""
    location: str = ""
    source_location: str = ""
    permanent: bool = False
    content: str | None = None


class FileSkill(SkillBase):
    """Route common file-management commands to the shared file backend."""

    def __init__(
        self,
        *,
        file_manager: FileManager | None = None,
        safety_policy: FileSafetyPolicy | None = None,
        smart_search: SmartFileSearch | None = None,
    ) -> None:
        self._files = file_manager or FileManager()
        self._safety = safety_policy or FileSafetyPolicy(path_resolver=self._files.resolver)
        self._search = smart_search or SmartFileSearch(
            file_manager=self._files,
            path_resolver=self._files.resolver,
            file_index=FileIndex(),
        )
        self._confirm_cb: Callable[[str], bool] | None = None

    def set_confirm_callback(self, callback: Callable[[str], bool]) -> None:
        self._confirm_cb = callback

    def ensure_index_ready_async(self, *, force: bool = False) -> None:
        self._search.ensure_index_ready_async(force=force)

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if self._has_pending_file_choices():
            if self._looks_like_file_choice_reply(command):
                return True

        pending = self._pending_request()
        if pending is not None:
            if self._looks_like_pending_reply(command, pending):
                return True

        normalized_intent = str(intent or "").strip().lower()
        parsed = self._parse_command(command)
        if parsed is None:
            if not settings.get("smart_file_search_enabled"):
                return False
            return self._looks_like_smart_search(command, normalized_intent)

        if not settings.get("file_operations_enabled"):
            return False

        if normalized_intent == "open_app":
            return False

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        if self._has_pending_file_choices():
            if self._looks_like_file_choice_reply(command):
                return self._handle_pending_file_choice(command)
            self._clear_pending_file_choices()

        if not settings.get("file_operations_enabled"):
            if not settings.get("smart_file_search_enabled"):
                return self._failure("File operations are disabled in settings.", "file_operations_disabled")

        pending = self._pending_request()
        if pending is not None:
            if self._looks_like_pending_reply(command, pending):
                return self._handle_pending_request(command, pending)
            self._clear_pending_request()

        parsed = self._parse_command(command)
        if parsed is not None:
            if not settings.get("file_operations_enabled"):
                return self._failure("File operations are disabled in settings.", "file_operations_disabled")
            return self._execute_parsed(parsed)

        if settings.get("smart_file_search_enabled") and self._looks_like_smart_search(
            command,
            str(context.get("intent") or "").strip().lower(),
        ):
            return self._execute_smart_search(command)

        return self._failure("I couldn't understand the file command.", "invalid_file_command")

    def execute_operation(
        self,
        operation: str,
        **params: Any,
    ) -> SkillExecutionResult:
        if not settings.get("file_operations_enabled"):
            return self._failure("File operations are disabled in settings.", "file_operations_disabled")
        parsed = ParsedFileCommand(
            action=str(operation or "").strip().lower(),
            reference=str(params.get("reference") or params.get("filename") or params.get("source_path") or params.get("path") or "").strip(),
            destination=str(params.get("destination") or params.get("target_path") or "").strip(),
            new_name=str(params.get("new_name") or "").strip(),
            location=str(params.get("location") or "").strip(),
            source_location=str(params.get("source_location") or "").strip(),
            permanent=bool(params.get("permanent")),
            content=params.get("content"),
        )
        resolved_source = str(params.get("resolved_source") or "").strip()
        source_path = Path(resolved_source) if resolved_source else None
        return self._execute_parsed(parsed, resolved_source=source_path, confirmed=bool(params.get("confirmed")))

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "files",
            "supports": [
                "create",
                "open",
                "rename",
                "delete",
                "move",
                "smart_search",
                "disambiguation",
                "path_resolution",
                "confirmation_flow",
            ],
        }

    def health_check(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.get("file_operations_enabled")),
            "pending_confirmation": bool(self._pending_request()),
            "pending_file_choices": bool(self._has_pending_file_choices()),
            "safe_delete_default": bool(settings.get("safe_delete_default")),
            "file_index_ready": bool(getattr(state, "file_index_ready", False)),
        }

    def _execute_parsed(
        self,
        parsed: ParsedFileCommand,
        *,
        resolved_source: Path | None = None,
        confirmed: bool = False,
    ) -> SkillExecutionResult:
        logger.info("File command received: %s", parsed)
        try:
            if parsed.action == "create":
                return self._execute_create(parsed, confirmed=confirmed)
            if parsed.action == "open":
                return self._execute_open(parsed, resolved_source=resolved_source)
            if parsed.action == "rename":
                return self._execute_rename(parsed, resolved_source=resolved_source, confirmed=confirmed)
            if parsed.action == "delete":
                return self._execute_delete(parsed, resolved_source=resolved_source, confirmed=confirmed)
            if parsed.action == "move":
                return self._execute_move(parsed, resolved_source=resolved_source, confirmed=confirmed)
            return self._failure(f"Unsupported file action: {parsed.action or 'unknown'}.", "unsupported_file_action")
        except Exception as exc:
            logger.error("File command failed: %s", exc)
            return self._failure(str(exc) or "File command failed.", "file_action_failed")

    def _execute_create(self, parsed: ParsedFileCommand, *, confirmed: bool) -> SkillExecutionResult:
        if not parsed.reference:
            return self._failure("Create file requires a target path.", "missing_target")
        target = self._files.resolve_target_path(parsed.reference, location_hint=parsed.location or None)
        overwrite = target.exists()
        if overwrite:
            decision = self._safety.evaluate("create", target_path=target, overwrite=True)
            gate = self._confirm_or_defer(parsed, decision, resolved_source=None, confirmed=confirmed)
            if gate is not None:
                return gate

        result = self._files.create_file(
            target,
            content=str(parsed.content) if parsed.content is not None else None,
            overwrite=overwrite and confirmed,
        )
        return self._finalize_result("file_create", result)

    def _execute_open(
        self,
        parsed: ParsedFileCommand,
        *,
        resolved_source: Path | None,
    ) -> SkillExecutionResult:
        if not parsed.reference and resolved_source is None:
            return self._failure("Open file requires a file or folder name.", "missing_target")
        source = resolved_source or self._resolve_single_match(parsed)
        if isinstance(source, SkillExecutionResult):
            return source
        result = self._files.open_file(source)
        return self._finalize_result("file_open", result)

    def _execute_rename(
        self,
        parsed: ParsedFileCommand,
        *,
        resolved_source: Path | None,
        confirmed: bool,
    ) -> SkillExecutionResult:
        if not parsed.reference and resolved_source is None:
            return self._failure("Rename requires a source file or folder.", "missing_target")
        if not parsed.new_name:
            return self._failure("Rename requires the new file or folder name.", "missing_new_name")
        source = resolved_source or self._resolve_single_match(parsed)
        if isinstance(source, SkillExecutionResult):
            return source

        preview_target = source.with_name(parsed.new_name)
        overwrite = preview_target.exists() and str(preview_target).lower() != str(source).lower()
        if overwrite:
            decision = self._safety.evaluate("rename", source_path=source, target_path=preview_target, overwrite=True)
            gate = self._confirm_or_defer(parsed, decision, resolved_source=source, confirmed=confirmed)
            if gate is not None:
                return gate

        result = self._files.rename_file(source, parsed.new_name, overwrite=overwrite and confirmed)
        return self._finalize_result("file_rename", result)

    def _execute_delete(
        self,
        parsed: ParsedFileCommand,
        *,
        resolved_source: Path | None,
        confirmed: bool,
    ) -> SkillExecutionResult:
        if not parsed.reference and resolved_source is None:
            return self._failure("Delete requires a file or folder name.", "missing_target")
        source = resolved_source or self._resolve_single_match(parsed)
        if isinstance(source, SkillExecutionResult):
            return source

        permanent = parsed.permanent or not settings.get("safe_delete_default")
        item_count = self._files.count_items(source) if source.exists() and source.is_dir() else 1
        decision = self._safety.evaluate(
            "delete",
            source_path=source,
            permanent=permanent,
            item_count=item_count,
        )
        gate = self._confirm_or_defer(parsed, decision, resolved_source=source, confirmed=confirmed)
        if gate is not None:
            return gate

        result = self._files.delete_file(source, permanent=permanent)
        return self._finalize_result("file_delete", result)

    def _execute_move(
        self,
        parsed: ParsedFileCommand,
        *,
        resolved_source: Path | None,
        confirmed: bool,
    ) -> SkillExecutionResult:
        if not parsed.reference and resolved_source is None:
            return self._failure("Move requires a source file or folder.", "missing_target")
        if not parsed.destination:
            return self._failure("Move requires a destination.", "missing_destination")
        source = resolved_source or self._resolve_single_match(parsed)
        if isinstance(source, SkillExecutionResult):
            return source

        preview_target = self._files.preview_move_target(source, parsed.destination)
        overwrite = preview_target.exists()
        decision = self._safety.evaluate(
            "move",
            source_path=source,
            target_path=preview_target,
            overwrite=overwrite,
        )
        gate = self._confirm_or_defer(parsed, decision, resolved_source=source, confirmed=confirmed)
        if gate is not None:
            return gate

        result = self._files.move_file(source, parsed.destination, overwrite=overwrite and confirmed)
        return self._finalize_result("file_move", result)

    def _resolve_single_match(self, parsed: ParsedFileCommand) -> Path | SkillExecutionResult:
        location_hint = parsed.source_location or parsed.location or None
        matches = self._files.find_matches(parsed.reference, location_hint=location_hint)
        logger.info("Resolved path candidates for %s: %s", parsed.reference, [str(match) for match in matches])
        if not matches:
            return self._failure(f"I couldn't find '{parsed.reference}'.", "file_not_found")
        if len(matches) == 1:
            return matches[0]
        return self._request_choice(parsed, matches)

    def _confirm_or_defer(
        self,
        parsed: ParsedFileCommand,
        decision,
        *,
        resolved_source: Path | None,
        confirmed: bool,
    ) -> SkillExecutionResult | None:
        if not decision.requires_confirmation or confirmed:
            return None

        if self._confirm_cb is not None:
            if self._confirm_cb(decision.prompt):
                return self._execute_parsed(parsed, resolved_source=resolved_source, confirmed=True)
            return self._cancelled("Cancelled the file operation.")

        self._store_pending_request(
            {
                "skill": "files",
                "kind": "confirm",
                "prompt": decision.prompt,
                "parsed": asdict(parsed),
                "resolved_source": str(resolved_source or ""),
            }
        )
        return SkillExecutionResult(
            success=False,
            intent=f"file_{parsed.action}",
            response=f"{decision.prompt} Say yes to continue or no to cancel.",
            skill_name=self.name(),
            error="confirmation_required",
            data={"target_app": "files"},
        )

    def _request_choice(self, parsed: ParsedFileCommand, matches: list[Path]) -> SkillExecutionResult:
        choices = [self._files.resolver.describe_path(match) for match in matches]
        lines = [f"{index}. {label}" for index, label in enumerate(choices, start=1)]
        prompt = "I found multiple matches:\n" + "\n".join(lines) + "\nReply with the number to continue, or say cancel."
        self._store_pending_request(
            {
                "skill": "files",
                "kind": "choice",
                "prompt": prompt,
                "choices": [str(match) for match in matches],
                "parsed": asdict(parsed),
            }
        )
        return SkillExecutionResult(
            success=False,
            intent=f"file_{parsed.action}",
            response=prompt,
            skill_name=self.name(),
            error="multiple_matches",
            data={"target_app": "files", "choices": choices},
        )

    def _handle_pending_request(self, command: str, pending: dict[str, Any]) -> SkillExecutionResult:
        normalized = self._normalize(command)
        if normalized in _NO_WORDS:
            self._clear_pending_request()
            return self._cancelled("Cancelled the pending file operation.")

        if pending.get("kind") == "confirm":
            if normalized not in _YES_WORDS:
                return SkillExecutionResult(
                    success=False,
                    intent="file_confirmation",
                    response=str(pending.get("prompt") or "Please answer yes or no."),
                    skill_name=self.name(),
                    error="confirmation_required",
                    data={"target_app": "files"},
                )

            self._clear_pending_request()
            parsed = ParsedFileCommand(**dict(pending.get("parsed") or {}))
            resolved_source = str(pending.get("resolved_source") or "").strip()
            return self._execute_parsed(
                parsed,
                resolved_source=Path(resolved_source) if resolved_source else None,
                confirmed=True,
            )

        if pending.get("kind") == "choice":
            if normalized.isdigit():
                index = int(normalized) - 1
                choices = list(pending.get("choices") or [])
                if 0 <= index < len(choices):
                    self._clear_pending_request()
                    parsed = ParsedFileCommand(**dict(pending.get("parsed") or {}))
                    return self._execute_parsed(parsed, resolved_source=Path(choices[index]))
            return SkillExecutionResult(
                success=False,
                intent="file_choice",
                response=str(pending.get("prompt") or "Please choose one of the listed files."),
                skill_name=self.name(),
                error="selection_required",
                data={"target_app": "files"},
            )

        self._clear_pending_request()
        return self._failure("The pending file request expired.", "pending_request_missing")

    def _finalize_result(self, intent: str, result: FileActionResult) -> SkillExecutionResult:
        if result.success:
            self._clear_pending_request()
            self._clear_pending_file_choices()
            self._remember_action(result)
        return SkillExecutionResult(
            success=result.success,
            intent=intent,
            response=result.message,
            skill_name=self.name(),
            error=result.error,
            data={
                "target_app": "files",
                "action": result.action,
                "source_path": result.source_path,
                "target_path": result.target_path,
                "timestamp": result.timestamp,
            },
        )

    def _remember_action(self, result: FileActionResult) -> None:
        state.last_file_action = result.action
        state.last_file_path = result.target_path or result.source_path
        state.last_destination_path = result.target_path
        recent = list(getattr(state, "recent_files_touched", []) or [])
        entry = {
            "action": result.action,
            "source_path": result.source_path,
            "target_path": result.target_path,
            "timestamp": result.timestamp,
        }
        recent.append(entry)
        limit = int(settings.get("file_recent_history_limit") or 10)
        state.recent_files_touched = recent[-limit:]

    def _execute_smart_search(self, command: str) -> SkillExecutionResult:
        response = self._search.search(command)
        self._remember_search(response.query.to_dict(), response.results)

        if not response.results:
            self._clear_pending_file_choices()
            return SkillExecutionResult(
                success=False,
                intent="file_search",
                response=response.message,
                skill_name=self.name(),
                error="file_not_found",
                data={"target_app": "files", "scope": response.scope, "results": []},
            )

        if response.query.open_after_search:
            best = self._search.choose_best(response.results, response.query)
            if best is not None:
                result = self._search.open_result(best)
                return self._finalize_result("file_open", result)
            return self._request_search_choice(response.results, prompt_prefix="I found multiple matching files to open:")

        self._store_pending_file_choices(response.results)
        return SkillExecutionResult(
            success=True,
            intent="file_search",
            response=self._format_search_results(response.results, intro="I found matching files:"),
            skill_name=self.name(),
            data={
                "target_app": "files",
                "scope": response.scope,
                "results": [result.to_dict() for result in response.results],
                "partial": response.partial,
            },
        )

    def _request_search_choice(self, results: list[SearchResult], *, prompt_prefix: str) -> SkillExecutionResult:
        self._store_pending_file_choices(results)
        return SkillExecutionResult(
            success=False,
            intent="file_search",
            response=self._format_search_results(results, intro=prompt_prefix, include_prompt=True),
            skill_name=self.name(),
            error="multiple_matches",
            data={"target_app": "files", "results": [result.to_dict() for result in results]},
        )

    def _handle_pending_file_choice(self, command: str) -> SkillExecutionResult:
        normalized = self._normalize(command)
        if normalized in _NO_WORDS:
            self._clear_pending_file_choices()
            return self._cancelled("Cancelled the pending file selection.")

        choice_index = self._extract_ordinal(normalized)
        choices = list(getattr(state, "pending_file_choices", []) or [])
        if choice_index is None or not (1 <= choice_index <= len(choices)):
            return SkillExecutionResult(
                success=False,
                intent="file_search",
                response="Please choose one of the listed files by number, or say cancel.",
                skill_name=self.name(),
                error="selection_required",
                data={"target_app": "files", "results": choices},
            )

        selected = choices[choice_index - 1]
        result = self._files.open_file(str(selected.get("path") or ""))
        return self._finalize_result("file_open", result)

    def _pending_request(self) -> dict[str, Any] | None:
        pending = getattr(state, "pending_confirmation", {}) or {}
        if pending.get("skill") != "files":
            return None
        return dict(pending)

    def _store_pending_request(self, payload: dict[str, Any]) -> None:
        state.pending_confirmation = payload

    def _clear_pending_request(self) -> None:
        if getattr(state, "pending_confirmation", {}) and state.pending_confirmation.get("skill") == "files":
            state.pending_confirmation = {}

    def _has_pending_file_choices(self) -> bool:
        return bool(getattr(state, "pending_file_choices", []) or [])

    def _store_pending_file_choices(self, results: list[SearchResult]) -> None:
        state.pending_file_choices = [result.to_dict() for result in results]

    def _clear_pending_file_choices(self) -> None:
        state.pending_file_choices = []

    def _looks_like_pending_reply(self, command: str, pending: Mapping[str, Any]) -> bool:
        normalized = self._normalize(command)
        if pending.get("kind") == "choice":
            return normalized.isdigit() or normalized in _NO_WORDS
        return normalized in _YES_WORDS or normalized in _NO_WORDS

    def _looks_like_file_choice_reply(self, command: str) -> bool:
        normalized = self._normalize(command)
        return normalized in _NO_WORDS or self._extract_ordinal(normalized) is not None

    def _looks_like_smart_search(self, command: str, normalized_intent: str) -> bool:
        if normalized_intent not in {"", "search", "search_file", "open_app", "unknown", "file_action"}:
            return False
        if self._looks_like_media_command(command) and not self._has_strong_file_evidence(command):
            return False
        return is_probable_file_search(command)

    @staticmethod
    def _has_strong_file_evidence(command: str) -> bool:
        lowered = str(command or "").strip().lower()
        return bool(
            re.search(r"\b(file|folder|document|documents|downloads|desktop|pdf|docx?|xlsx?|txt|zip|resume|cv)\b", lowered)
            or re.search(r"\.[a-z0-9]{1,6}\b", lowered)
            or re.search(r"\b(search|find|open)\s+.+\b(file|folder|document|pdf|docx?|xlsx?|txt|resume|cv)\b", lowered)
        )

    @staticmethod
    def _looks_like_media_command(command: str) -> bool:
        return bool(_MEDIA_COMMAND_PATTERN.search(str(command or "").strip()))

    def _parse_command(self, command: str) -> ParsedFileCommand | None:
        cleaned = str(command or "").strip()
        if not cleaned:
            return None

        extracted = entities.extract_file_action(cleaned)
        if extracted.get("action") and extracted.get("action") != "unknown":
            return ParsedFileCommand(
                action=str(extracted.get("action") or "").strip().lower(),
                reference=str(extracted.get("filename") or "").strip(),
                destination=str(extracted.get("destination") or "").strip(),
                new_name=str(extracted.get("new_name") or "").strip(),
                location=str(extracted.get("location") or "").strip(),
                source_location=str(extracted.get("source_location") or "").strip(),
                permanent=bool(extracted.get("permanent")),
                content=str(extracted.get("content") or "").strip() or None,
            )

        lowered = cleaned.lower().strip()
        if lowered.startswith(("open ", "launch ", "start ")) and entities.looks_like_file_reference(cleaned):
            tail = cleaned.split(" ", 1)[1]
            reference, location = entities.split_reference_and_location(tail)
            return ParsedFileCommand(action="open", reference=reference, location=location, source_location=location)

        return None

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="file_action",
            response=response,
            skill_name=self.name(),
            error=error,
            data={"target_app": "files"},
        )

    def _cancelled(self, response: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="file_action",
            response=response,
            skill_name=self.name(),
            error="cancelled",
            data={"target_app": "files"},
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return normalize_command(text)

    def _remember_search(self, query: dict[str, Any], results: list[SearchResult]) -> None:
        state.last_file_search_query = query
        state.last_file_search_results = [result.to_dict() for result in results]

    def _format_search_results(
        self,
        results: list[SearchResult],
        *,
        intro: str,
        include_prompt: bool = False,
    ) -> str:
        lines = [intro]
        for index, result in enumerate(results, start=1):
            label = result.name
            path_label = self._files.resolver.describe_path(Path(result.path))
            if path_label.lower() != result.name.lower():
                label = f"{label} ({path_label})"
            lines.append(f"{index}. {label}")
        if include_prompt:
            lines.append("Which one should I open?")
        return "\n".join(lines)

    @staticmethod
    def _extract_ordinal(text: str) -> int | None:
        return _shared_extract_ordinal(text)
