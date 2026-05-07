"""
Production-grade local LLM integration via Ollama.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from threading import RLock
from typing import Any, TypeVar

import requests
from pydantic import BaseModel, ValidationError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core import settings, state
from core.logger import get_logger
from core.paths import DATA_DIR
from core.prompts import (
    build_intent_prompt,
    build_plan_prompt,
    build_stt_correction_prompt,
)
from core.schemas import CorrectedCommand, IntentSchema, PlanSchema

logger = get_logger(__name__)

SchemaModelT = TypeVar("SchemaModelT", bound=BaseModel)

DEFAULT_CACHE_PATH = DATA_DIR / "llm_cache.json"
DEFAULT_AVAILABILITY_TTL_SECONDS = 10.0
DEFAULT_JSON_ATTEMPTS = 2
DEFAULT_MAX_CACHE_ENTRIES = 256
DEFAULT_MAX_PROMPT_CHARS = 24000
DEFAULT_FALLBACK_MODELS = ("llama3", "qwen3.5", "qwen3", "mistral", "phi3")
DEFAULT_HEALTHCHECK_TIMEOUT = (0.5, 1.5)
DEFAULT_LIST_MODELS_TIMEOUT = (0.5, 2.0)
DEFAULT_INTENT_TIMEOUT_SECONDS = 8.0


class LLMClient:
    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        temperature: float | None = None,
        enabled: bool | None = None,
        session: requests.Session | None = None,
        cache_path: str | Path | None = None,
        auto_health_check: bool = False,
    ) -> None:
        self.enabled = bool(settings.get("llm_enabled")) if enabled is None else bool(enabled)
        self.host = self._normalize_host(host or settings.get("llm_host") or "http://localhost:11434")
        self.requested_model = (model or settings.get("llm_model") or "llama3").strip()
        self.model = self.requested_model
        self.timeout = int(timeout or settings.get("llm_timeout") or 30)
        self.temperature = float(
            self._default_temperature() if temperature is None else temperature
        )
        self.session = session or requests.Session()
        self.cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
        self._availability_checked_at = 0.0
        self._cache_lock = RLock()
        self._cache = self._load_cache()

        # Circuit breaker for failing servers
        self._failure_count = 0
        self._circuit_broken_until = 0.0
        self._circuit_break_threshold = 3  # after 3 failures
        self._circuit_break_duration = 30.0  # seconds

        self._configure_session()

        if self.enabled and auto_health_check:
            self.health_check()
        elif self.enabled:
            state.llm_ready = False
            state.last_llm_model = ""
            logger.info("Local LLM availability will be checked on demand")
        else:
            state.llm_ready = False
            logger.info("Local LLM support is disabled in settings")

    def is_available(self, force_refresh: bool = False) -> bool:
        if not self.enabled:
            return False

        # Circuit breaker: avoid hammering a dead server
        if time.monotonic() < self._circuit_broken_until:
            logger.debug("LLM circuit breaker active, service considered unavailable")
            return False

        if force_refresh or (time.monotonic() - self._availability_checked_at) > DEFAULT_AVAILABILITY_TTL_SECONDS:
            return self.health_check()
        return state.llm_ready

    def list_models(self, force_refresh: bool = False) -> list[str]:
        if not self.enabled:
            return []

        cache_key = self._cache_key("list_models", {"host": self.host})
        if not force_refresh:
            cached = self._cache_get(cache_key)
            if isinstance(cached, list):
                return [str(model) for model in cached]

        try:
            payload, _ = self._request_json(
                "GET",
                "tags",
                task="list_models",
                timeout=DEFAULT_LIST_MODELS_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("Failed to list Ollama models: %s", self._format_request_error(exc))
            return []

        models = []
        for model_entry in payload.get("models", []):
            model_name = model_entry.get("name") or model_entry.get("model")
            if model_name:
                models.append(str(model_name))

        self._cache_set(cache_key, models)
        return models

    def pull_model(self, name: str) -> dict[str, Any] | None:
        if not self.enabled or not name.strip():
            return None

        try:
            payload, latency = self._request_json(
                "POST",
                "pull",
                payload={"model": name.strip(), "stream": False},
                task="pull_model",
                timeout=max(self.timeout, 300),
            )
        except requests.RequestException as exc:
            logger.error("Failed to pull Ollama model '%s': %s", name, self._format_request_error(exc))
            return None

        self._record_success("pull_model", latency, payload)
        return payload

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float | None = 0,
        format: str | dict[str, Any] | None = None,
        *,
        task: str = "generate",
        use_cache: bool = True,
    ) -> str | None:
        prompt = self._sanitize_prompt(prompt)
        if prompt is None or not self.is_available():
            return None

        resolved_temperature = self._resolve_temperature(temperature)
        cache_key = self._cache_key(
            task,
            {
                "mode": "generate",
                "model": self.model,
                "prompt": prompt,
                "system": system or "",
                "temperature": resolved_temperature,
                "format": format,
            },
        )
        if use_cache:
            cached = self._cache_get(cache_key)
            if isinstance(cached, str):
                logger.info("LLM cache hit: %s", task)
                self._record_success(task, 0.0, cached)
                return cached

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": resolved_temperature},
        }
        if system:
            payload["system"] = system
        if format is not None:
            payload["format"] = format

        logger.info("LLM task: %s", task)
        try:
            response_data, latency = self._request_json("POST", "generate", payload=payload, task=task)
        except requests.RequestException as exc:
            logger.error("LLM generate failed: %s", self._format_request_error(exc))
            return None

        response_text = str(response_data.get("response", "")).strip()
        if not response_text:
            logger.error("LLM generate returned an empty response")
            return None

        self._record_success(task, latency, response_text)
        if use_cache:
            self._cache_set(cache_key, response_text)
        return response_text

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = 0,
        format: str | dict[str, Any] | None = None,
        *,
        task: str = "chat",
        use_cache: bool = True,
        timeout: int | float | tuple[float, float] | None = None,
    ) -> str | None:
        if not messages or not self.is_available():
            return None

        prompt_size = sum(len(str(message.get("content", ""))) for message in messages)
        if prompt_size > DEFAULT_MAX_PROMPT_CHARS:
            state.last_error = (
                f"Prompt too large for the local model ({prompt_size} chars > {DEFAULT_MAX_PROMPT_CHARS})."
            )
            logger.warning(state.last_error)
            return None

        resolved_temperature = self._resolve_temperature(temperature)
        cache_key = self._cache_key(
            task,
            {
                "mode": "chat",
                "model": self.model,
                "messages": messages,
                "temperature": resolved_temperature,
                "format": format,
            },
        )
        if use_cache:
            cached = self._cache_get(cache_key)
            if isinstance(cached, str):
                logger.info("LLM cache hit: %s", task)
                self._record_success(task, 0.0, cached)
                return cached

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": resolved_temperature},
        }
        if format is not None:
            payload["format"] = format

        logger.info("LLM task: %s", task)
        try:
            response_data, latency = self._request_json(
                "POST",
                "chat",
                payload=payload,
                task=task,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            logger.error("LLM chat failed: %s", self._format_request_error(exc))
            return None

        response_text = str(response_data.get("message", {}).get("content", "")).strip()
        if not response_text:
            logger.error("LLM chat returned an empty response")
            return None

        self._record_success(task, latency, response_text)
        if use_cache:
            self._cache_set(cache_key, response_text)
        return response_text

    def json_generate(
        self,
        prompt: str,
        schema: type[SchemaModelT] | dict[str, Any] | None = None,
        *,
        system: str | None = None,
        temperature: float | None = 0,
        task: str = "json_generate",
        max_attempts: int | None = None,
        timeout: int | float | tuple[float, float] | None = None,
    ) -> SchemaModelT | dict[str, Any] | list[Any] | None:
        prompt = self._sanitize_prompt(prompt)
        if prompt is None or not self.is_available():
            return None

        schema_payload = self._resolve_schema(schema)
        system_prompt = self._compose_json_system_prompt(system=system, schema_text=schema_payload["schema_text"])
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        raw_response = ""
        last_error = ""

        attempts = max(1, int(max_attempts or DEFAULT_JSON_ATTEMPTS))

        for attempt in range(1, attempts + 1):
            attempt_task = f"{task}_attempt_{attempt}"
            current_messages = messages
            if attempt > 1:
                current_messages = [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": self._build_json_retry_prompt(
                            prompt=prompt,
                            schema_text=schema_payload["schema_text"],
                            previous_output=raw_response,
                            validation_error=last_error or "Invalid JSON response.",
                        ),
                    },
                ]

            raw_response = self.chat(
                current_messages,
                temperature=temperature,
                format=schema_payload["format"],
                task=attempt_task,
                use_cache=(attempt == 1),
                timeout=timeout,
            ) or ""

            if not raw_response:
                continue

            parsed = self._extract_json(raw_response)
            if parsed is None:
                last_error = "Response was not valid JSON."
                logger.warning("Parsed JSON failure for %s: %s", attempt_task, last_error)
                continue

            validated, validation_error = self._validate_json_payload(parsed, schema)
            if validation_error is None:
                logger.info("Parsed JSON success for %s", task)
                return validated

            last_error = validation_error
            logger.warning("Parsed JSON failure for %s: %s", attempt_task, validation_error)

        logger.error("Failed to produce valid structured JSON for %s", task)
        return None

    def health_check(self) -> bool:
        if not self.enabled:
            state.llm_ready = False
            return False

        logger.info("LLM task: health_check")
        try:
            version_payload, latency = self._request_json(
                "GET",
                "version",
                task="health_check",
                timeout=DEFAULT_HEALTHCHECK_TIMEOUT,
            )
        except requests.RequestException as exc:
            message = self._format_request_error(exc)
            state.llm_ready = False
            state.last_error = message
            self._availability_checked_at = time.monotonic()

            # Circuit breaker: increment failure count
            self._failure_count += 1
            if self._failure_count >= self._circuit_break_threshold:
                self._circuit_broken_until = time.monotonic() + self._circuit_break_duration
                logger.error(
                    "LLM circuit breaker activated after %d failures. "
                    "Pausing requests for %.0f seconds.",
                    self._failure_count,
                    self._circuit_break_duration
                )

            logger.warning("Ollama not reachable: %s", message)
            return False

        logger.info("Ollama reachable")

        # Reset circuit breaker on successful health check
        self._failure_count = 0
        self._circuit_broken_until = 0.0

        self._record_success("health_check", latency, version_payload)

        models = self.list_models(force_refresh=False)
        resolved_model = self._resolve_model(models)
        if not resolved_model:
            models = self.list_models(force_refresh=True)
            resolved_model = self._resolve_model(models)
        self._availability_checked_at = time.monotonic()

        if resolved_model:
            self.model = resolved_model
            state.llm_ready = True
            state.last_llm_model = resolved_model
            logger.info("Model: %s", resolved_model)
            return True

        state.llm_ready = False
        state.last_llm_model = ""
        logger.warning(
            "Configured model '%s' is not installed. Available models: %s",
            self.requested_model,
            ", ".join(models) if models else "none",
        )
        return False

    def correct_stt(self, text: str) -> CorrectedCommand | None:
        result = self.json_generate(
            build_stt_correction_prompt(text),
            schema=CorrectedCommand,
            task="stt_correction",
        )
        return result if isinstance(result, CorrectedCommand) else None

    def extract_intent(self, text: str) -> IntentSchema | None:
        result = self.json_generate(
            build_intent_prompt(text),
            schema=IntentSchema,
            task="intent_extraction",
            max_attempts=1,
            timeout=min(float(self.timeout), DEFAULT_INTENT_TIMEOUT_SECONDS),
        )
        return result if isinstance(result, IntentSchema) else None

    def plan_actions(self, text: str, context: dict[str, Any] | None = None) -> PlanSchema | None:
        result = self.json_generate(
            build_plan_prompt(text, context=context),
            schema=PlanSchema,
            task="planning",
        )
        return result if isinstance(result, PlanSchema) else None

    def _default_temperature(self) -> float:
        value = settings.get("llm_temperature")
        return float(value if value is not None else 0.0)

    def _configure_session(self) -> None:
        # Retry only idempotent GETs. Retrying POST-based generation causes
        # large latency spikes and leaves timed-out work running in the
        # background after the UI has already recovered.
        retry = Retry(
            total=2,
            connect=2,
            read=1,
            status=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        if hasattr(self.session, "mount"):
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        task: str,
        timeout: int | float | tuple[float, float] | None = None,
    ) -> tuple[dict[str, Any], float]:
        # Check circuit breaker
        if time.monotonic() < self._circuit_broken_until:
            raise requests.ConnectionError("LLM service circuit breaker is active")

        start = time.perf_counter()
        try:
            response = self.session.request(
                method=method.upper(),
                url=self._endpoint(path),
                json=payload,
                timeout=timeout or self.timeout,
            )
            response.raise_for_status()
            latency = time.perf_counter() - start
            try:
                data = response.json()
            except ValueError as exc:
                raise requests.RequestException(f"{task} returned non-JSON output") from exc
            return data, latency
        except requests.RequestException:
            # Increment failure counter on request failures
            self._failure_count += 1
            raise

    def _record_success(self, task: str, latency: float, output: Any) -> None:
        state.last_llm_task = task
        state.last_llm_latency = latency
        state.last_llm_output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=True)
        logger.info("Response time: %.2f sec", latency)

    def _sanitize_prompt(self, prompt: str) -> str | None:
        normalized = (prompt or "").strip()
        if not normalized:
            state.last_error = "Cannot send an empty prompt to the local model."
            logger.warning(state.last_error)
            return None
        if len(normalized) > DEFAULT_MAX_PROMPT_CHARS:
            state.last_error = (
                f"Prompt too large for the local model ({len(normalized)} chars > {DEFAULT_MAX_PROMPT_CHARS})."
            )
            logger.warning(state.last_error)
            return None
        return normalized

    def _resolve_temperature(self, temperature: float | None) -> float:
        if temperature is None:
            return self.temperature
        return float(temperature)

    def _resolve_model(self, installed_models: list[str]) -> str | None:
        if not installed_models:
            return None

        candidates = [self.requested_model, *DEFAULT_FALLBACK_MODELS]
        for candidate in candidates:
            match = self._find_model_match(candidate, installed_models)
            if match:
                if candidate != self.requested_model:
                    logger.warning(
                        "Configured model '%s' is missing. Falling back to '%s'.",
                        self.requested_model,
                        match,
                    )
                return match
        return None

    @staticmethod
    def _find_model_match(requested_model: str, installed_models: list[str]) -> str | None:
        requested = requested_model.strip().lower()
        requested_base = requested.split(":", 1)[0]

        for model_name in installed_models:
            normalized = model_name.lower()
            if normalized == requested:
                return model_name
            if normalized.split(":", 1)[0] == requested_base:
                return model_name
        return None

    @staticmethod
    def _normalize_host(host: str) -> str:
        normalized = host.strip().rstrip("/")
        if normalized.endswith("/api"):
            normalized = normalized[:-4]
        return normalized

    def _endpoint(self, path: str) -> str:
        clean_path = path.lstrip("/")
        if not clean_path.startswith("api/"):
            clean_path = f"api/{clean_path}"
        return f"{self.host}/{clean_path}"

    def _resolve_schema(self, schema: type[SchemaModelT] | dict[str, Any] | None) -> dict[str, Any]:
        if schema is None:
            return {"format": "json", "schema_text": ""}
        if isinstance(schema, dict):
            return {"format": schema, "schema_text": json.dumps(schema, indent=2, ensure_ascii=True)}
        if issubclass(schema, BaseModel):
            schema_dict = schema.model_json_schema()
            return {"format": schema_dict, "schema_text": json.dumps(schema_dict, indent=2, ensure_ascii=True)}
        raise TypeError("schema must be a Pydantic model class, dict, or None.")

    @staticmethod
    def _compose_json_system_prompt(system: str | None, schema_text: str) -> str:
        parts = [
            "Return machine-usable JSON only.",
            "Do not include markdown fences, commentary, or explanatory text.",
        ]
        if system:
            parts.append(system.strip())
        if schema_text:
            parts.append(f"Follow this JSON schema exactly:\n{schema_text}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_json_retry_prompt(
        *,
        prompt: str,
        schema_text: str,
        previous_output: str,
        validation_error: str,
    ) -> str:
        return (
            "Your previous response was invalid.\n"
            f"Validation error: {validation_error}\n\n"
            "Return only corrected JSON.\n\n"
            f"Schema:\n{schema_text or 'Return valid JSON.'}\n\n"
            f"Original task:\n{prompt}\n\n"
            f"Previous invalid output:\n{previous_output}"
        )

    def _validate_json_payload(
        self,
        payload: Any,
        schema: type[SchemaModelT] | dict[str, Any] | None,
    ) -> tuple[SchemaModelT | dict[str, Any] | list[Any] | None, str | None]:
        if schema is None or isinstance(schema, dict):
            return payload, None
        try:
            validated = schema.model_validate(payload)
        except ValidationError as exc:
            return None, str(exc)
        return validated, None

    @staticmethod
    def _extract_json(raw_text: str) -> dict[str, Any] | list[Any] | None:
        candidates: list[str] = []
        stripped = raw_text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                stripped = "\n".join(lines[1:-1]).strip()
        candidates.append(stripped)

        first_object = stripped.find("{")
        last_object = stripped.rfind("}")
        if first_object != -1 and last_object != -1 and last_object > first_object:
            candidates.append(stripped[first_object : last_object + 1].strip())

        first_array = stripped.find("[")
        last_array = stripped.rfind("]")
        if first_array != -1 and last_array != -1 and last_array > first_array:
            candidates.append(stripped[first_array : last_array + 1].strip())

        decoder = json.JSONDecoder()
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                try:
                    parsed, _ = decoder.raw_decode(candidate)
                    return parsed
                except json.JSONDecodeError:
                    continue
        return None

    def _load_cache(self) -> dict[str, Any]:
        try:
            if not self.cache_path.exists():
                self.cache_path.write_text("{}", encoding="utf-8")
                return {}
            raw_cache = json.loads(self.cache_path.read_text(encoding="utf-8") or "{}")
            return raw_cache if isinstance(raw_cache, dict) else {}
        except OSError as exc:
            logger.warning("Failed to initialize LLM cache: %s", exc)
            return {}
        except json.JSONDecodeError:
            logger.warning("LLM cache is corrupt; starting with an empty cache.")
            return {}

    def _cache_get(self, key: str) -> Any:
        with self._cache_lock:
            entry = self._cache.get(key)
            if not isinstance(entry, dict):
                return None
            return entry.get("value")

    def _cache_set(self, key: str, value: Any) -> None:
        with self._cache_lock:
            self._cache[key] = {"value": value, "timestamp": time.time()}
            if len(self._cache) > DEFAULT_MAX_CACHE_ENTRIES:
                oldest_keys = sorted(
                    self._cache.keys(),
                    key=lambda entry_key: float(self._cache[entry_key].get("timestamp", 0)),
                )
                for oldest_key in oldest_keys[:-DEFAULT_MAX_CACHE_ENTRIES]:
                    self._cache.pop(oldest_key, None)
            try:
                self.cache_path.write_text(
                    json.dumps(self._cache, indent=2, ensure_ascii=True),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("Failed to persist LLM cache: %s", exc)

    @staticmethod
    def _cache_key(task: str, payload: dict[str, Any]) -> str:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"{task}:{digest}"

    @staticmethod
    def _format_request_error(exc: requests.RequestException) -> str:
        if isinstance(exc, requests.Timeout):
            return "The Ollama request timed out."
        if isinstance(exc, requests.ConnectionError):
            return "Unable to connect to the Ollama server."

        response = getattr(exc, "response", None)
        if response is not None:
            try:
                payload = response.json()
                error_message = payload.get("error") or payload.get("message")
            except ValueError:
                error_message = response.text.strip() or None
            if error_message:
                if "memory" in error_message.lower():
                    return f"Ollama reported a memory error: {error_message}"
                return str(error_message)

        return str(exc) or "Unknown Ollama error."
