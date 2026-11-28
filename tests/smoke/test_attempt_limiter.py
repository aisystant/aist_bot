"""WP-384: core/attempt_limiter.py - failed attempts per key in a sliding window."""

from core.attempt_limiter import FailureLimiter


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_blocks_only_after_the_limit_of_failures_is_reached():
    clock = _Clock()
    limiter = FailureLimiter(max_failures=3, window_sec=60, clock=clock)
    for _ in range(2):
        limiter.record("1.2.3.4")
    assert not limiter.blocked("1.2.3.4")
    limiter.record("1.2.3.4")
    assert limiter.blocked("1.2.3.4")


def test_keys_are_independent():
    limiter = FailureLimiter(max_failures=1, window_sec=60, clock=_Clock())
    limiter.record("1.2.3.4")
    assert limiter.blocked("1.2.3.4")
    assert not limiter.blocked("5.6.7.8")


def test_failures_expire_with_the_window():
    clock = _Clock()
    limiter = FailureLimiter(max_failures=2, window_sec=60, clock=clock)
    limiter.record("k")
    limiter.record("k")
    assert limiter.blocked("k")
    clock.now += 61
    assert not limiter.blocked("k")
    limiter.record("k")
    assert not limiter.blocked("k")


def test_old_failures_do_not_count_toward_a_new_burst():
    clock = _Clock()
    limiter = FailureLimiter(max_failures=3, window_sec=60, clock=clock)
    limiter.record("k")
    limiter.record("k")
    clock.now += 61
    limiter.record("k")
    assert not limiter.blocked("k")


def test_tracked_keys_are_bounded():
    import core.attempt_limiter as module

    limiter = FailureLimiter(max_failures=5, window_sec=60, clock=_Clock())
    for i in range(module._MAX_TRACKED_KEYS + 5):
        limiter.record(f"ip-{i}")
    assert len(limiter._failures) <= module._MAX_TRACKED_KEYS
