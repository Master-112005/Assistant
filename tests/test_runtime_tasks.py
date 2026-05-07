import threading

from core.runtime_tasks import invoke_with_timeout


def test_invoke_with_timeout_uses_daemon_worker():
    outcome = invoke_with_timeout(lambda: threading.current_thread().daemon, timeout_seconds=1)

    assert outcome.completed is True
    assert outcome.error is None
    assert outcome.value is True
