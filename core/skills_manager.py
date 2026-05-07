"""
Skill/plugin registration and routing.
"""
from __future__ import annotations

from typing import Any, Mapping

from core import settings, state
from core.app_commands import parse_app_command
from core.browser import BrowserController
from core.logger import get_logger
from core.permissions import permission_manager as default_permission_manager
from core.plugin_manager import PluginManager
from core.lazy_utils import LazyGetter
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)

_SKILL_FACTORIES: dict[str, callable] = {}


class SkillsManager:
    """Registry and router for built-in and future plugin skills."""

    def __init__(
        self,
        *,
        browser_controller: BrowserController | None = None,
        launcher=None,
        permission_manager=None,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        self.browser_controller = browser_controller or BrowserController(launcher=launcher)
        self.launcher = launcher
        self.permission_manager = permission_manager or default_permission_manager
        self.plugin_manager = plugin_manager if plugin_manager is not None else (
            PluginManager(permission_manager=self.permission_manager) if settings.get("plugins_enabled") else None
        )
        self._skills: dict[str, SkillBase] = {}
        self._skill_factories: dict[str, callable] = {}
        self._builtins_loaded = False
        self._confirm_callback = None

        for skill_name in (
            "ReminderSkill", "FileSkill", "ClipboardSkill", "YouTubeSkill",
            "MusicSkill", "ChromeSkill", "WhatsAppSkill", "SystemSkill",
            "ClickTextSkill", "AwarenessSkill", "OCRSkill",
        ):
            self._skill_factories[skill_name] = None

    def register(self, skill: SkillBase) -> None:
        skill_name = skill.name()
        self._skills[skill_name] = skill
        if hasattr(skill, "set_permission_manager"):
            skill.set_permission_manager(self.permission_manager)
        if self._confirm_callback is not None and hasattr(skill, "set_confirm_callback"):
            skill.set_confirm_callback(self._confirm_callback)
        logger.info("Registered skill: %s", skill_name)

    def load_builtin_skills(self) -> None:
        if self._builtins_loaded:
            return

        from skills.chrome import ChromeSkill
        from skills.click_text import ClickTextSkill
        from skills.awareness import AwarenessSkill
        from skills.clipboard import ClipboardSkill
        from skills.files import FileSkill
        from skills.music import MusicSkill
        from skills.ocr import OCRSkill
        from skills.reminders import ReminderSkill
        from skills.system import SystemSkill
        from skills.whatsapp import WhatsAppSkill
        from skills.youtube import YouTubeSkill

        self._skill_factories = {
            "ReminderSkill": lambda: ReminderSkill(),
            "FileSkill": lambda: FileSkill(),
            "ClipboardSkill": lambda: ClipboardSkill(),
            "YouTubeSkill": lambda: YouTubeSkill(controller=self.browser_controller),
            "MusicSkill": lambda: MusicSkill(launcher=self.launcher),
            "ChromeSkill": lambda: ChromeSkill(controller=self.browser_controller),
            "WhatsAppSkill": lambda: WhatsAppSkill(launcher=self.launcher, browser=self.browser_controller),
            "SystemSkill": lambda: SystemSkill(),
            "ClickTextSkill": lambda: ClickTextSkill(),
            "AwarenessSkill": lambda: AwarenessSkill(),
            "OCRSkill": lambda: OCRSkill(),
        }

        self._builtins_loaded = True
        logger.info("Skill factories registered: %s", ", ".join(self._skill_factories.keys()))
        self._load_plugins_on_startup()

    def _ensure_skill_loaded(self, name: str) -> None:
        if name in self._skills:
            return
        factory = self._skill_factories.get(name)
        if factory:
            skill = factory()
            self.register(skill)
            logger.debug("Lazy loaded skill: %s", name)

    def find_skill(
        self,
        context: Mapping[str, Any],
        intent: str,
        command: str,
    ) -> SkillBase | None:
        """
        Find the appropriate skill for a command based on routing.

        CRITICAL: This method enforces strict routing boundaries:
        - "launcher" route -> NO skill interception (goes to AppLauncher directly)
        - "unknown" route -> NO skill interception (goes to fallback)
        - Dedicated routes (whatsapp, youtube, spotify) -> specific skills only
        - "browser" route -> browser skill with strict validation
        """
        target_route = context.get("target_route")
        entities = context.get("entities") if isinstance(context.get("entities"), Mapping) else {}
        
        # FORCED ROUTING: Check if this is a WhatsApp communication intent
        # Even if routing returns "unknown", force WhatsAppSkill for these intents
        forced_whatsapp = intent in {"call_contact", "send_message", "whatsapp_call", "whatsapp_message"}
        command_lower = command.lower() if command else ""
        if "whatsapp" in command_lower or forced_whatsapp:
            logger.debug("[SKILLS:FIND:WHATSAPP_FORCE] Forcing WhatsApp for intent=%s", intent)
            target_route = "whatsapp"
        
        logger.debug(
            "[SKILLS:FIND] Searching for skill: route='%s', intent='%s', command='%s'",
            target_route,
            intent,
            command[:60] if command else "",
        )

        if target_route:
            logger.debug(
                "[SKILLS:FIND:ROUTED] Using explicit route: '%s'",
                target_route,
            )
            route_map = {
                "youtube": "YouTubeSkill",
                "spotify": "MusicSkill",
                "whatsapp": "WhatsAppSkill",
                "reminders": "ReminderSkill",
                "click": "ClickTextSkill",
                "ocr": "OCRSkill",
                "file": "FileSkill",
                "system": "SystemSkill",
                "browser": "ChromeSkill",  # Only when explicitly routed to browser
            }

            # CRITICAL: Launcher and unknown routes must NOT be intercepted by skills
            # This prevents ChromeSkill from stealing "open whatsapp" commands
            if target_route in {"launcher", "unknown"}:
                logger.debug(
                    "[SKILLS:FIND:BLOCKED] Route '%s' blocks skill interception",
                    target_route,
                )
                return None  # Let processor handle via AppLauncher or fallback

            skill_name = route_map.get(target_route)
            if skill_name:
                logger.debug(
                    "[SKILLS:FIND:ROUTE_MAPPED] Route '%s' maps to skill '%s'",
                    target_route,
                    skill_name,
                )
                self._ensure_skill_loaded(skill_name)
                skill = self._skills.get(skill_name)
                logger.debug("[SKILLS:FIND] Looking for skill '%s', found=%s", skill_name, skill is not None)
                if skill:
                    # For dedicated routes (whatsapp, youtube, spotify), use skill directly
                    if target_route in {"whatsapp", "youtube", "spotify", "reminders"}:
                        logger.info(
                            "[SKILLS:FIND:SELECTED] Skill matched: '%s' for route '%s'",
                            skill_name,
                            target_route,
                        )
                        return skill
                    # For browser skill, add extra validation to prevent false positives
                    if skill_name == "ChromeSkill":
                        if not self._validate_browser_skill_match(context, intent, command, entities):
                            logger.debug(
                                "[SKILLS:FIND:BROWSER_BLOCKED] ChromeSkill blocked by validation",
                            )
                            return None
                    if skill.can_handle(context, intent, command):
                        logger.info(
                            "[SKILLS:FIND:SELECTED] Skill matched: '%s' for route '%s'",
                            skill_name,
                            target_route,
                        )
                        return skill
                    logger.debug(
                        "[SKILLS:FIND:CANT_HANDLE] Skill '%s' exists but can_handle=False",
                        skill_name,
                    )
                # If route has a dedicated skill but it can't handle, DON'T fall back
                # This is deterministic routing - the route decides, not skill.can_handle
                logger.debug(
                    "[SKILLS:FIND:ROUTE_STRICT] Route '%s' has no matching skill, no fallback",
                    target_route,
                )
                return None

        # No explicit route - allow skill matching for context-based commands
        # But STILL protect against browser skill intercepting app commands
        logger.debug(
            "[SKILLS:FIND:GENERIC] No explicit route, trying context-based skill matching",
        )
        for skill in self._skills.values():
            try:
                if skill.can_handle(context, intent, command):
                    # Extra protection: don't let ChromeSkill handle app open commands
                    if skill.name() == "ChromeSkill" and self._is_app_open_command(intent, command, entities):
                        logger.debug(
                            "[SKILLS:FIND:CHROME_BLOCKED] ChromeSkill blocked for app open command",
                        )
                        continue
                    logger.info(
                        "[SKILLS:FIND:MATCHED] Skill matched by can_handle: '%s'",
                        skill.name(),
                    )
                    return skill
            except Exception as exc:
                logger.warning("Skill can_handle failed for %s: %s", skill.name(), exc)
        
        logger.debug(
            "[SKILLS:FIND:NONE] No skill found for this command",
        )
        return None

    def _validate_browser_skill_match(
        self,
        context: Mapping[str, Any],
        intent: str,
        command: str,
        entities: Mapping[str, Any],
    ) -> bool:
        """
        Validate that ChromeSkill should actually handle this command.

        Prevents false positives where ChromeSkill intercepts:
        - "open whatsapp" (should go to launcher/WhatsAppSkill)
        - "open spotify" (should go to spotify/MusicSkill)
        - Any app open command for non-browser apps
        """
        # Check if this is an app open command for a non-browser app
        if self._is_app_open_command(intent, command, entities):
            app_name = self._extract_app_from_command(command, entities)
            # Only allow ChromeSkill for actual browser apps
            browser_apps = {"chrome", "edge", "firefox", "brave", "opera", "vivaldi", "safari", "browser"}
            return app_name.lower() in browser_apps if app_name else False

        return True

    def _is_app_open_command(self, intent: str, command: str, entities: Mapping[str, Any]) -> bool:
        """Check if this is an app open/launch command."""
        open_intents = {"open_app", "close_app", "minimize_app", "maximize_app", "focus_app", "restore_app", "toggle_app"}
        if intent in open_intents:
            return True

        command_lower = str(command or "").strip().lower()
        open_verbs = ("open ", "launch ", "start ", "run ", "close ", "minimize ", "maximize ", "focus ", "toggle ")
        return any(command_lower.startswith(verb) for verb in open_verbs)

    def _extract_app_from_command(self, command: str, entities: Mapping[str, Any]) -> str:
        """Extract app name from command or entities."""
        # Try entities first
        for key in ("app", "app_name", "target_app"):
            val = entities.get(key) if isinstance(entities, Mapping) else None
            if val and isinstance(val, str) and val.strip():
                return val.strip().lower()

        parsed = parse_app_command(str(command or ""))
        if parsed is not None:
            return parsed.app_name

        return ""

    def execute_with_skill(
        self,
        context: Mapping[str, Any],
        intent: str,
        command: str,
    ) -> SkillExecutionResult | None:
        skill = self.find_skill(context, intent, command)
        if skill is None:
            state.active_skill = ""
            return self._execute_with_plugin(command, context)

        state.active_skill = skill.name()
        state.last_skill_used = skill.name()
        logger.info("Skill: %s", skill.name())

        if skill.name() in {"FileSkill", "SystemSkill"}:
            result = skill.execute(command, context)
        else:
            result = self._execute_with_permissions(skill, command, context, intent)
        if not result.skill_name:
            result.skill_name = skill.name()

        logger.info("Skill %s handled command: %s", skill.name(), command)
        return result

    def list_skills(self) -> list[dict[str, Any]]:
        return [
            {
                "name": skill.name(),
                "capabilities": skill.get_capabilities(),
                "health": skill.health_check(),
            }
            for skill in self._skills.values()
        ]

    def get_skill(self, name: str) -> SkillBase | None:
        return self._skills.get(str(name or "").strip())

    def set_confirm_callback(self, callback) -> None:
        self._confirm_callback = callback
        self.permission_manager.set_confirmation_callback(callback)
        for skill in self._skills.values():
            if hasattr(skill, "set_confirm_callback"):
                skill.set_confirm_callback(callback)

    @property
    def file_skill(self):
        return self._skills.get("FileSkill")

    @property
    def clipboard_skill(self):
        return self._skills.get("ClipboardSkill")

    @property
    def reminder_skill(self):
        return self._skills.get("ReminderSkill")

    @property
    def system_skill(self):
        return self._skills.get("SystemSkill")

    def _execute_with_permissions(
        self,
        skill: SkillBase,
        command: str,
        context: Mapping[str, Any],
        intent: str,
    ) -> SkillExecutionResult:
        permission_result = self.permission_manager.evaluate(
            skill.name(),
            {
                "command": command,
                "intent": intent,
                "entities": dict(context.get("entities") or {}) if isinstance(context.get("entities"), dict) else {},
                "target_app": context.get("context_target_app") or context.get("current_app"),
            },
        )
        if permission_result.decision == permission_result.decision.DENY:
            return SkillExecutionResult(
                success=False,
                intent=intent or "permission_denied",
                response=permission_result.reason,
                skill_name=skill.name(),
                error="permission_denied",
                data={"target_app": "permissions", "permission": permission_result.to_dict()},
            )

        if permission_result.decision == permission_result.decision.REQUIRE_CONFIRMATION:
            prompt = f"{permission_result.risk_level.value.title()} action: {command}"
            token = self.permission_manager.request_confirmation(
                skill.name(),
                {
                    "prompt": prompt,
                    "reason": permission_result.reason,
                    "risk_level": permission_result.risk_level,
                    "params": {
                        "command": command,
                        "intent": intent,
                        "entities": dict(context.get("entities") or {}) if isinstance(context.get("entities"), dict) else {},
                    },
                },
                callback=lambda: skill.execute(command, {**dict(context), "permission_prechecked": True}),
            )
            if self._confirm_callback is not None and self._confirm_callback(prompt):
                self.permission_manager.approve(token)
                result = skill.execute(command, {**dict(context), "permission_prechecked": True})
                self.permission_manager.record_execution(skill.name(), {"intent": intent}, success=result.success, error=result.error)
                return result
            if self._confirm_callback is not None:
                self.permission_manager.deny(token)
                return SkillExecutionResult(
                    success=False,
                    intent=intent or "permission_confirmation",
                    response="Cancelled the action.",
                    skill_name=skill.name(),
                    error="cancelled",
                    data={"target_app": "permissions"},
                )
            return SkillExecutionResult(
                success=False,
                intent=intent or "permission_confirmation",
                response=f"{prompt} Say yes to continue or cancel to stop.",
                skill_name=skill.name(),
                error="confirmation_required",
                data={"target_app": "permissions", "token": token},
            )

        result = skill.execute(command, {**dict(context), "permission_prechecked": True})
        self.permission_manager.record_execution(skill.name(), {"intent": intent}, success=result.success, error=result.error)
        return result

    def _load_plugins_on_startup(self) -> None:
        if self.plugin_manager is None:
            state.plugins_ready = False
            return
        try:
            self.plugin_manager.discover_plugins()
            if settings.get("plugins_auto_load"):
                self.plugin_manager.load_all_enabled()
            state.plugins_ready = True
        except Exception as exc:
            state.plugins_ready = False
            logger.exception("Plugin startup load failed", exc=exc)

    def _execute_with_plugin(
        self,
        command: str,
        context: Mapping[str, Any],
    ) -> SkillExecutionResult | None:
        if self.plugin_manager is None:
            return None
        result = self.plugin_manager.route(command, context)
        if result is None:
            return None
        state.active_skill = result.skill_name
        state.last_skill_used = result.skill_name
        logger.info("Plugin skill handled command", skill_name=result.skill_name, command=command)
        return result
