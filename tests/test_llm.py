import json
import requests

from core import state
from core.llm import LLMClient
from core.schemas import CorrectedCommand, IntentSchema, PlanSchema


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(self.text)
            error.response = self
            raise error


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def mount(self, *_args, **_kwargs):
        return None

    def request(self, method, url, json=None, timeout=None):
        self.requests.append({"method": method, "url": url, "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError("Unexpected request with no prepared response.")
        next_response = self.responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


def _build_client(tmp_path, responses, auto_health_check=False):
    session = FakeSession(responses)
    client = LLMClient(
        session=session,
        cache_path=tmp_path / "llm_cache.json",
        auto_health_check=auto_health_check,
    )
    return client, session


def _mark_available(client):
    client.is_available = lambda force_refresh=False: True


def test_client_does_not_health_check_on_construction_by_default(tmp_path):
    session = FakeSession(
        [
            FakeResponse({"version": "0.12.6"}),
            FakeResponse({"models": [{"name": "llama3"}]}),
        ]
    )

    client = LLMClient(session=session, cache_path=tmp_path / "llm_cache.json")

    assert isinstance(client, LLMClient)
    assert session.requests == []
    assert state.llm_ready is False


def test_health_check_detects_ollama_server(tmp_path):
    client, _session = _build_client(
        tmp_path,
        [
            FakeResponse({"version": "0.12.6"}),
            FakeResponse({"models": [{"name": "llama3"}]}),
        ],
    )

    assert client.health_check() is True
    assert state.llm_ready is True


def test_health_check_detects_installed_model(tmp_path):
    client, _session = _build_client(
        tmp_path,
        [
            FakeResponse({"version": "0.12.6"}),
            FakeResponse({"models": [{"name": "llama3:latest"}, {"name": "mistral"}]}),
        ],
    )

    assert client.health_check() is True
    assert state.last_llm_model == "llama3:latest"


def test_health_check_falls_back_to_qwen_when_requested_model_is_missing(tmp_path):
    client, _session = _build_client(
        tmp_path,
        [
            FakeResponse({"version": "0.12.6"}),
            FakeResponse({"models": [{"name": "qwen3.5:latest"}]}),
        ],
    )

    assert client.health_check() is True
    assert state.last_llm_model == "qwen3.5:latest"


def test_stt_correction_returns_valid_schema(tmp_path):
    client, _session = _build_client(
        tmp_path,
        [
            FakeResponse(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "original_text": "open app music an pley liked music",
                                "corrected_text": "Open Apple Music and play liked songs",
                                "confidence": 0.93,
                            }
                        )
                    }
                }
            )
        ],
    )
    _mark_available(client)

    result = client.correct_stt("open app music an pley liked music")

    assert isinstance(result, CorrectedCommand)
    assert result.corrected_text == "Open Apple Music and play liked songs"


def test_intent_extraction_handles_multi_action(tmp_path):
    client, _session = _build_client(
        tmp_path,
        [
            FakeResponse(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "multi_action",
                                "confidence": 0.95,
                                "entities": {"apps": ["chrome"], "query": "IPL score"},
                                "reason": "Contains two actions",
                            }
                        )
                    }
                }
            )
        ],
    )
    _mark_available(client)

    result = client.extract_intent("open chrome and search IPL score")

    assert isinstance(result, IntentSchema)
    assert result.intent == "multi_action"
    assert result.entities["apps"] == ["chrome"]


def test_planning_returns_ordered_steps(tmp_path):
    client, _session = _build_client(
        tmp_path,
        [
            FakeResponse(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "steps": [
                                    {
                                        "order": 2,
                                        "action": "search",
                                        "target": "YouTube",
                                        "params": {"query": "today's IPL result"},
                                    },
                                    {
                                        "order": 1,
                                        "action": "open_app",
                                        "target": "YouTube",
                                        "params": {},
                                    },
                                ]
                            }
                        )
                    }
                }
            )
        ],
    )
    _mark_available(client)

    result = client.plan_actions("Open YouTube and search today's IPL result")

    assert isinstance(result, PlanSchema)
    assert [step.order for step in result.steps] == [1, 2]
    assert result.steps[0].action == "open_app"


def test_invalid_json_retry_works(tmp_path):
    client, session = _build_client(
        tmp_path,
        [
            FakeResponse({"message": {"content": "```json\n{invalid}\n```"}}),
            FakeResponse(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "original_text": "open app music an pley liked music",
                                "corrected_text": "Open Apple Music and play liked songs",
                                "confidence": 0.93,
                            }
                        )
                    }
                }
            ),
        ],
    )
    _mark_available(client)

    result = client.correct_stt("open app music an pley liked music")

    assert isinstance(result, CorrectedCommand)
    assert len(session.requests) == 2


def test_extract_intent_fails_fast_on_schema_validation_error(tmp_path):
    client, session = _build_client(
        tmp_path,
        [
            FakeResponse(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "search",
                                "confidence": 1.2,
                                "entities": {"query": "ipl score"},
                                "reason": "Invalid confidence for schema",
                            }
                        )
                    }
                }
            ),
        ],
    )
    _mark_available(client)

    result = client.extract_intent("search for IPL score")

    assert result is None
    assert len(session.requests) == 1


def test_repeated_calls_are_stable_and_use_cache(tmp_path):
    client, session = _build_client(
        tmp_path,
        [
            FakeResponse(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "search",
                                "confidence": 0.91,
                                "entities": {"query": "ipl score"},
                                "reason": "Detected a search request",
                            }
                        )
                    }
                }
            )
        ],
    )
    _mark_available(client)

    first = client.extract_intent("search for IPL score")
    second = client.extract_intent("search for IPL score")

    assert isinstance(first, IntentSchema)
    assert isinstance(second, IntentSchema)
    assert first.model_dump() == second.model_dump()
    assert len(session.requests) == 1


def test_extract_intent_uses_single_attempt_and_short_timeout(tmp_path):
    client, session = _build_client(
        tmp_path,
        [
            FakeResponse(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "search",
                                "confidence": 0.91,
                                "entities": {"query": "ipl score"},
                                "reason": "Detected a search request",
                            }
                        )
                    }
                }
            )
        ],
    )
    _mark_available(client)

    result = client.extract_intent("search for IPL score")

    assert isinstance(result, IntentSchema)
    assert len(session.requests) == 1
    assert session.requests[0]["timeout"] == 8.0


def test_no_server_is_handled_gracefully(tmp_path):
    client, _session = _build_client(
        tmp_path,
        [requests.ConnectionError("connection refused")],
    )

    assert client.health_check() is False
    assert client.generate("hello") is None
