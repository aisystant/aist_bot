"""Transport of the Knock adapter: time limits, retry, the "unknown outcome" rule."""
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from knock_adapter.models import Outcome
from knock_adapter.transport import AdapterConfig, KnockTransport, default_opener
from tests.knock_adapter_fakes import FakeClock, ScriptedOpener, json_response


def make(steps, **config):
    clock = FakeClock()
    opener = ScriptedOpener(clock, steps)
    transport = KnockTransport("test-key", AdapterConfig(**config), opener=opener, sleep=clock.sleep,
                               clock=clock, rng=random.Random(7))
    return transport, opener, clock


def test_success_returns_data_and_sends_runtime_key():
    transport, opener, _ = make([json_response(200, {"id": "u1"})])
    result = transport.request("GET", "/users/u1", safe_to_retry=True)
    assert (result.outcome, result.data, result.attempts) == (Outcome.OK, {"id": "u1"}, 1)
    assert opener.calls[0][3]["Authorization"] == "Bearer test-key"


def test_rate_limit_with_retry_after_waits_exactly_that_long():
    transport, opener, clock = make([json_response(429, {}, {"Retry-After": "3"}), json_response(200, {})])
    result = transport.request("POST", "/schedules", {}, safe_to_retry=False)
    assert result.outcome is Outcome.OK and result.attempts == 2
    assert clock.sleeps == [3.0]


def test_rate_limit_without_header_grows_the_pause_with_jitter():
    transport, _, clock = make([json_response(429)] * 2 + [json_response(200, {})])
    transport.request("PUT", "/users/u1", {}, safe_to_retry=False)
    assert 0.75 <= clock.sleeps[0] <= 1.25
    assert 1.5 <= clock.sleeps[1] <= 2.5


def test_retry_after_is_capped():
    transport, _, clock = make([json_response(429, {}, {"Retry-After": "500"}), json_response(200, {})])
    transport.request("GET", "/x", safe_to_retry=True)
    assert clock.sleeps == [60.0]


def test_rate_limit_forever_ends_as_retryable_after_max_attempts():
    transport, opener, _ = make([json_response(429)] * 5)
    result = transport.request("POST", "/schedules", {}, safe_to_retry=False)
    assert (result.outcome, result.reason, result.attempts) == (Outcome.RETRYABLE_ERROR, "rate_limited", 5)
    assert len(opener.calls) == 5


def test_mutating_call_that_times_out_is_not_repeated():
    transport, opener, clock = make([TimeoutError("timed out")])
    key = ("create_schedule", "p1", "r1")
    result = transport.request("POST", "/schedules", {}, safe_to_retry=False, reconcile_key=key)
    assert result.outcome is Outcome.OUTCOME_UNKNOWN and result.reason == "timeout"
    assert result.reconcile_key == key and len(opener.calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("step, reason", [
    (json_response(503), "http_503"),
    (ConnectionResetError("reset"), "connection_error"),
])
def test_mutating_call_with_uncertain_end_returns_unknown_outcome(step, reason):
    transport, opener, _ = make([step])
    result = transport.request("POST", "/workflows/w/trigger", {}, safe_to_retry=False)
    assert (result.outcome, result.reason, len(opener.calls)) == (Outcome.OUTCOME_UNKNOWN, reason, 1)


def test_unreadable_body_after_success_status_is_unknown_for_mutations():
    transport, _, _ = make([(200, {}, b"{not json")])
    result = transport.request("POST", "/schedules", {}, safe_to_retry=False)
    assert (result.outcome, result.reason) == (Outcome.OUTCOME_UNKNOWN, "invalid_json")


def test_safe_call_repeats_after_server_error_and_succeeds():
    transport, _, _ = make([json_response(500), json_response(200, {"ok": 1})])
    result = transport.request("GET", "/users/u1", safe_to_retry=True)
    assert (result.outcome, result.attempts) == (Outcome.OK, 2)


def test_safe_call_that_always_hangs_ends_as_timeout_within_five_attempts():
    transport, opener, _ = make([TimeoutError("timed out")] * 5)
    result = transport.request("GET", "/users/u1", safe_to_retry=True)
    assert (result.outcome, result.attempts, len(opener.calls)) == (Outcome.TIMEOUT, 5, 5)


def test_total_budget_includes_time_spent_inside_the_calls():
    transport, opener, clock = make([TimeoutError("timed out")] * 5, total_budget=25.0, jitter=0.0)
    result = transport.request("GET", "/users/u1", safe_to_retry=True)
    assert result.outcome is Outcome.TIMEOUT and result.attempts == 3
    assert [round(call[2], 3) for call in opener.calls] == [10.0, 10.0, 2.0]
    assert clock.t <= 25.0 + 1e-9


def test_batch_calls_use_the_longer_timeout():
    transport, opener, _ = make([json_response(200, {})])
    transport.request("POST", "/users/bulk/identify", {}, safe_to_retry=True, batch=True)
    assert opener.calls[0][2] == 30.0


def test_other_client_errors_are_final_and_not_repeated():
    transport, opener, _ = make([json_response(422, {"error": "bad"})])
    result = transport.request("POST", "/schedules", {}, safe_to_retry=False)
    assert (result.outcome, result.reason, result.status_code) == (Outcome.PERMANENT_ERROR, "http_422", 422)
    assert len(opener.calls) == 1


def test_not_found_is_success_only_when_the_caller_asks_for_it():
    transport, _, _ = make([json_response(404), json_response(404)])
    deleted = transport.request("DELETE", "/users/u9", safe_to_retry=True, not_found_ok=True)
    read = transport.request("GET", "/users/u9", safe_to_retry=True)
    assert (deleted.outcome, deleted.reason) == (Outcome.OK, "already_absent")
    assert (read.outcome, read.reason) == (Outcome.PERMANENT_ERROR, "not_found")


def test_in_flight_calls_never_exceed_the_concurrency_limit():
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def opener(request, timeout):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.03)
        with lock:
            state["now"] -= 1
        return json_response(200, {})

    transport = KnockTransport("k", AdapterConfig(max_concurrency=2, bulk_concurrency=2), opener=opener)
    threads = [threading.Thread(target=transport.request, args=("GET", "/x"), kwargs={"safe_to_retry": True})
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["max"] == 2


def test_unserializable_body_is_a_final_result_and_nothing_is_sent():
    transport, opener, _ = make([])
    result = transport.request("POST", "/workflows/w/trigger", {"data": object()}, safe_to_retry=False)
    assert (result.outcome, result.reason) == (Outcome.PERMANENT_ERROR, "invalid_request")
    assert opener.calls == []


def test_no_new_attempt_starts_when_a_real_sleep_overshoots_the_budget():
    clock = FakeClock()
    opener = ScriptedOpener(clock, [json_response(429), json_response(200, {})])
    transport = KnockTransport("k", AdapterConfig(total_budget=20.0), opener=opener,
                               sleep=lambda seconds: clock.advance(1000.0), clock=clock)
    result = transport.request("GET", "/x", safe_to_retry=True)
    assert (result.outcome, result.attempts, len(opener.calls)) == (Outcome.RETRYABLE_ERROR, 1, 1)


def test_configuration_is_validated():
    with pytest.raises(ValueError):
        AdapterConfig(call_timeout=61.0)
    with pytest.raises(ValueError):
        AdapterConfig(max_concurrency=1, bulk_concurrency=2)
    with pytest.raises(ValueError):
        KnockTransport("", AdapterConfig())


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path == "/hang":
            time.sleep(1.0)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"pong": true}')

    do_PUT = do_GET

    def log_message(self, *args):
        pass


@pytest.fixture()
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:{}".format(server.server_address[1])
    server.shutdown()
    server.server_close()


def test_real_http_success_and_hard_timeout(local_server):
    config = AdapterConfig(base_url=local_server, call_timeout=0.2, batch_timeout=0.2, total_budget=5.0,
                           pause_base=0.01)
    transport = KnockTransport("k", config, opener=default_opener)
    assert transport.request("GET", "/ping", safe_to_retry=True).data == {"pong": True}
    started = time.monotonic()
    hung = transport.request("PUT", "/hang", {}, safe_to_retry=False)
    assert (hung.outcome, hung.reason, hung.attempts) == (Outcome.OUTCOME_UNKNOWN, "timeout", 1)
    assert time.monotonic() - started < 0.9
