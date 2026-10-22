import pytest


@pytest.fixture(autouse=True)
def _alarm_backstop():
    """Backstop only: abort any single test that hangs for 20s (POSIX; needs SIGALRM)."""
    import signal

    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _on_alarm(signum, frame):
        raise TimeoutError("test exceeded 20s backstop timeout")

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(20)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
