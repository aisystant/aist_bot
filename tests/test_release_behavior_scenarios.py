"""BotBehaviorScenarioResolver: receipt shape, privacy, spec strictness (WP-562 Ф7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from release_manifest import loads_strict_json
from scripts.release_behavior_scenarios import (
    SPEC_PATH,
    BehaviorScenarioResolutionError,
    BotBehaviorScenarioResolver,
    load_behavior_scenarios,
    response_matches,
)
from scripts.release_control import (
    BehaviorScenarioVerificationRequest,
    CutoverBlocked,
    load_release_control_contract,
)
from tests.test_release_control import (
    CONTRACT_PATH,
    NOW,
    FakeClock,
    _contract,
    _execute,
    _plan,
)


def _request(scenario_ids: tuple[str, ...]) -> BehaviorScenarioVerificationRequest:
    contract = load_release_control_contract(CONTRACT_PATH)
    return BehaviorScenarioVerificationRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        manifest_release_id="release-2026-10-15-01",
        artifact_digest="sha256:" + "a" * 64,
        scenario_ids=scenario_ids,
    )


def _write_spec(tmp_path: Path, mutate) -> Path:
    spec = loads_strict_json(SPEC_PATH.read_bytes())
    mutate(spec)
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(spec, ensure_ascii=False))
    return path


def test_spec_scenarios_are_exactly_the_contract_scenarios() -> None:
    contract = load_release_control_contract(CONTRACT_PATH)
    ids = tuple(scenario.scenario_id for scenario in load_behavior_scenarios())

    assert ids == contract.behavior_scenarios


def test_resolver_passes_every_scenario_against_current_handlers() -> None:
    contract = load_release_control_contract(CONTRACT_PATH)
    request = _request(contract.behavior_scenarios)

    receipt = BotBehaviorScenarioResolver(clock=FakeClock(NOW)).resolve_verified(
        request
    )

    assert receipt.request_digest == request.request_digest
    assert receipt.verified_at == NOW
    assert [result.scenario_id for result in receipt.results] == list(
        contract.behavior_scenarios
    )
    assert all(result.passed for result in receipt.results)
    assert len({result.expectation_hash for result in receipt.results}) == len(
        receipt.results
    )


def test_receipt_carries_no_bot_text_or_synthetic_user_data() -> None:
    contract = load_release_control_contract(CONTRACT_PATH)
    receipt = BotBehaviorScenarioResolver(clock=FakeClock(NOW)).resolve_verified(
        _request(contract.behavior_scenarios)
    )

    serialized = json.dumps(receipt.to_mapping(), ensure_ascii=False)
    for leaked in (
        "Тестовый",
        "Привет",
        "Вопрос",
        "testuser",
        "synthetic-aisystant-id",
        "example.invalid",
        "₽",
    ):
        assert leaked not in serialized
    assert set(receipt.results[0].to_mapping()) == {
        "scenario_id",
        "passed",
        "expectation_hash",
    }


def test_unmet_expectation_reports_failed_scenario(tmp_path) -> None:
    def require_missing_button(spec) -> None:
        spec["scenarios"][0]["then"]["expect"]["inline_callback_data"] = [
            "button-that-does-not-exist"
        ]

    path = _write_spec(tmp_path, require_missing_button)
    contract = load_release_control_contract(CONTRACT_PATH)

    receipt = BotBehaviorScenarioResolver(
        clock=FakeClock(NOW), spec_path=path
    ).resolve_verified(_request(contract.behavior_scenarios))

    assert [result.passed for result in receipt.results] == [False, True, True]


def test_spec_drift_from_contract_raises() -> None:
    resolver = BotBehaviorScenarioResolver(clock=FakeClock(NOW))

    with pytest.raises(BehaviorScenarioResolutionError, match="differs"):
        resolver.resolve_verified(_request(("diagnose-first-question",)))


def test_real_resolver_satisfies_core_gate_before_cutover_stop() -> None:
    contract = _contract(cutover=False)
    operation = _plan(contract).operations[0]

    with pytest.raises(CutoverBlocked):
        _execute(
            contract=contract,
            operation=operation,
            behavior_resolver=BotBehaviorScenarioResolver(clock=FakeClock(NOW)),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda spec: spec["scenarios"].reverse(),
            "sorted",
        ),
        (
            lambda spec: spec["scenarios"][0]["given"].__setitem__(
                "profile", "unknown-profile"
            ),
            "unknown profile",
        ),
        (
            lambda spec: spec["scenarios"][0]["then"]["expect"].__setitem__(
                "exact_text", "Привет"
            ),
            "unknown keys",
        ),
        (
            lambda spec: spec["scenarios"][1]["then"]["expect"].__setitem__(
                "reply_keyboard", False
            ),
            "must be true",
        ),
        (
            lambda spec: spec["scenarios"][0].pop("source"),
            "must have keys",
        ),
        (
            lambda spec: spec["scenarios"][0]["when"].__setitem__("kind", "photo"),
            "unsupported",
        ),
    ],
    ids=[
        "unsorted",
        "unknown-profile",
        "exact-text",
        "false-flag",
        "no-source",
        "bad-kind",
    ],
)
def test_spec_loader_rejects_malformed_spec(tmp_path, mutate, message) -> None:
    path = _write_spec(tmp_path, mutate)

    with pytest.raises(BehaviorScenarioResolutionError, match=message):
        load_behavior_scenarios(path)


def _message(markup) -> dict:
    return {"method": "send_message", "text": "непустой", "reply_markup": markup}


def test_response_matching_is_structural_not_textual() -> None:
    inline = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="любой текст", callback_data="diag:cp.rhy:1:0")],
            [InlineKeyboardButton(text="оплата", url="https://pay.invalid/1")],
        ]
    )
    reply = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="меню")]])
    sent = [_message(reply), _message(inline)]

    assert response_matches(
        sent,
        {
            "send_message_min": 2,
            "reply_keyboard": True,
            "inline_callback_prefixes": ["diag:"],
            "inline_url_button": True,
        },
    )
    assert not response_matches(sent, {"send_message_min": 3})
    assert not response_matches(sent, {"inline_callback_data": ["path_marathon"]})
    assert not response_matches(sent, {"callback_answered": True})
    assert not response_matches([_message(None)], {"reply_keyboard": True})
    assert not response_matches(
        [{"method": "send_message", "text": "  ", "reply_markup": None}],
        {"send_message_min": 1},
    )
