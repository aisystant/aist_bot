"""Bot-backed VerifiedBehaviorScenarioResolverPort (WP-562 Ф7).

Single source of truth: `.github/release-behavior-scenarios.json` holds the
human-readable description of every scenario (given / when / then) together
with its machine-readable part (profile, update, structural expectation).
Both this resolver and `tests/smoke/test_behavior_scenarios.py` read that
file; neither keeps its own copy of the scenario list.

Each scenario is replayed in-process through aiogram with `ThinMockBot`
(no Telegram network) and synthetic user data; database and payment-API
calls are patched per profile. The comparison is structural only -- message
count, keyboard kind, callback_data, URL buttons -- never exact wording or
prices, so a copy edit cannot block a release. Bot text is read solely to
check it is non-empty and never leaves `response_matches`; the receipt
carries scenario IDs, pass/fail flags and a hash of the scenario spec.

Known limitation: scenarios exercise the handler code of the checked-out
source tree, not the built OCI artifact named in the request, and only the
routers the scenarios need (in production relative order) -- not the full
middleware stack from bot.py. The full pipeline stays covered by the
existing smoke regression check.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, Update

from release_manifest import canonical_sha256, loads_strict_json
from scripts.release_control import (
    BehaviorScenarioReceipt,
    BehaviorScenarioResult,
    BehaviorScenarioVerificationRequest,
    TrustedClockPort,
)

# Importing the smoke conftest also seeds the fake env (bot token, DB URL)
# that `config` requires before any handler module is imported.
from tests.smoke.conftest import ThinMockBot, make_intern
from tests.smoke.factories import callback_button, make_command_update

LOGGER = logging.getLogger(__name__)

SPEC_PATH = Path(__file__).resolve().parent.parent / (
    ".github/release-behavior-scenarios.json"
)
VERIFIER_IDENTITY = "bot-behavior-scenario-resolver.v1"
SYNTHETIC_CHAT_ID = 12345

_SPEC_KEYS = {"schema_version", "scenarios"}
_SCENARIO_KEYS = {"scenario_id", "source", "given", "when", "then"}
_GIVEN_KEYS = {"description", "profile"}
_WHEN_KEYS = {"description", "kind", "value"}
_THEN_KEYS = {"description", "expect"}


class BehaviorScenarioResolutionError(RuntimeError):
    """The scenario spec cannot be trusted or does not match the contract."""


@dataclass(frozen=True)
class BehaviorScenario:
    scenario_id: str
    profile: str
    when_kind: str
    when_value: str
    expect: Mapping[str, Any]
    spec_hash: str


# --- structural expectations -------------------------------------------------


def _inline_buttons(sent: Sequence[Mapping[str, Any]]) -> list[Any]:
    return [
        button
        for call in sent
        if isinstance(call.get("reply_markup"), InlineKeyboardMarkup)
        for row in call["reply_markup"].inline_keyboard
        for button in row
    ]


def _messages(sent: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [call for call in sent if call["method"] == "send_message"]


def _send_message_min(sent: Sequence[Mapping[str, Any]], minimum: int) -> bool:
    messages = _messages(sent)
    return len(messages) >= minimum and all(
        isinstance(message["text"], str) and message["text"].strip()
        for message in messages
    )


def _reply_keyboard(sent: Sequence[Mapping[str, Any]], _: bool) -> bool:
    return any(
        isinstance(message["reply_markup"], ReplyKeyboardMarkup)
        for message in _messages(sent)
    )


def _inline_callback_data(
    sent: Sequence[Mapping[str, Any]], required: Sequence[str]
) -> bool:
    present = {button.callback_data for button in _inline_buttons(sent)}
    return set(required) <= present


def _inline_callback_prefixes(
    sent: Sequence[Mapping[str, Any]], prefixes: Sequence[str]
) -> bool:
    data = [button.callback_data or "" for button in _inline_buttons(sent)]
    return all(any(item.startswith(prefix) for item in data) for prefix in prefixes)


def _inline_url_button(sent: Sequence[Mapping[str, Any]], _: bool) -> bool:
    return any(button.url for button in _inline_buttons(sent))


def _callback_answered(sent: Sequence[Mapping[str, Any]], _: bool) -> bool:
    return any(call["method"] == "answer_callback_query" for call in sent)


_EXPECTATIONS: dict[str, Callable[[Sequence[Mapping[str, Any]], Any], bool]] = {
    "callback_answered": _callback_answered,
    "inline_callback_data": _inline_callback_data,
    "inline_callback_prefixes": _inline_callback_prefixes,
    "inline_url_button": _inline_url_button,
    "reply_keyboard": _reply_keyboard,
    "send_message_min": _send_message_min,
}
_FLAG_EXPECTATIONS = {
    "callback_answered",
    "inline_url_button",
    "reply_keyboard",
}
_LIST_EXPECTATIONS = {"inline_callback_data", "inline_callback_prefixes"}


def response_matches(
    sent: Sequence[Mapping[str, Any]], expect: Mapping[str, Any]
) -> bool:
    """True when every structural expectation holds for the recorded calls."""

    return all(_EXPECTATIONS[name](sent, value) for name, value in expect.items())


# --- spec loading ------------------------------------------------------------


def _exact(value: Any, keys: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise BehaviorScenarioResolutionError(f"{path} must have keys {sorted(keys)}")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BehaviorScenarioResolutionError(f"{path} must be non-empty text")
    return value


def _validate_expect(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise BehaviorScenarioResolutionError(f"{path} must be a non-empty object")
    unknown = sorted(set(value) - set(_EXPECTATIONS))
    if unknown:
        raise BehaviorScenarioResolutionError(f"{path} has unknown keys {unknown}")
    for name, item in value.items():
        if name in _FLAG_EXPECTATIONS and item is not True:
            raise BehaviorScenarioResolutionError(f"{path}.{name} must be true")
        if name in _LIST_EXPECTATIONS and (
            not isinstance(item, list)
            or not item
            or any(not isinstance(entry, str) or not entry for entry in item)
        ):
            raise BehaviorScenarioResolutionError(
                f"{path}.{name} must be a non-empty list of strings"
            )
        if name == "send_message_min" and (type(item) is not int or item < 1):
            raise BehaviorScenarioResolutionError(f"{path}.{name} must be >= 1")
    return value


def _parse_scenario(entry: Any, index: int) -> BehaviorScenario:
    path = f"scenarios[{index}]"
    scenario = _exact(entry, _SCENARIO_KEYS, path)
    _text(scenario["source"], f"{path}.source")
    given = _exact(scenario["given"], _GIVEN_KEYS, f"{path}.given")
    when = _exact(scenario["when"], _WHEN_KEYS, f"{path}.when")
    then = _exact(scenario["then"], _THEN_KEYS, f"{path}.then")
    for section, name in ((given, "given"), (when, "when"), (then, "then")):
        _text(section["description"], f"{path}.{name}.description")
    profile = _text(given["profile"], f"{path}.given.profile")
    if profile not in _PROFILES:
        raise BehaviorScenarioResolutionError(f"{path} uses unknown profile")
    if when["kind"] not in _UPDATE_BUILDERS:
        raise BehaviorScenarioResolutionError(f"{path}.when.kind is unsupported")
    return BehaviorScenario(
        scenario_id=_text(scenario["scenario_id"], f"{path}.scenario_id"),
        profile=profile,
        when_kind=when["kind"],
        when_value=_text(when["value"], f"{path}.when.value"),
        expect=_validate_expect(then["expect"], f"{path}.then.expect"),
        spec_hash=canonical_sha256(scenario),
    )


def load_behavior_scenarios(path: Path = SPEC_PATH) -> tuple[BehaviorScenario, ...]:
    """Load and strictly validate the single scenario source file."""

    root = _exact(loads_strict_json(path.read_bytes()), _SPEC_KEYS, "spec")
    if root["schema_version"] != 1:
        raise BehaviorScenarioResolutionError("spec.schema_version must be 1")
    entries = root["scenarios"]
    if not isinstance(entries, list) or not entries:
        raise BehaviorScenarioResolutionError("spec.scenarios must be non-empty")
    scenarios = tuple(
        _parse_scenario(entry, index) for index, entry in enumerate(entries)
    )
    ids = [scenario.scenario_id for scenario in scenarios]
    if ids != sorted(set(ids)):
        raise BehaviorScenarioResolutionError("spec scenario IDs must be sorted")
    return scenarios


# --- profiles: synthetic "given" state, IO patched ---------------------------


def _new_participant() -> list[Any]:
    return [
        patch(
            "handlers.onboarding.get_intern",
            new_callable=AsyncMock,
            return_value=make_intern(onboarding_completed=False),
        ),
        patch("handlers.onboarding.update_intern", new_callable=AsyncMock),
        patch(
            "handlers.onboarding._try_auto_link",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "core.tier_detector.detect_ui_tier",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch("db.queries.events.log_event", new_callable=AsyncMock),
    ]


def _participant_without_assessment() -> list[Any]:
    return [
        patch(
            "helpers.dual_write.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "db.queries.get_intern",
            new_callable=AsyncMock,
            return_value=make_intern(onboarding_completed=True),
        ),
    ]


def _linked_participant_with_tariffs() -> list[Any]:
    tariff = {
        "code": "synthetic-monthly",
        "details": {"amount": 1, "periodicity": "m", "name": "synthetic"},
    }
    return [
        patch(
            "handlers.subscription.get_intern",
            new_callable=AsyncMock,
            return_value=make_intern(onboarding_completed=True),
        ),
        patch(
            "handlers.subscription.get_aisystant_id",
            new_callable=AsyncMock,
            return_value="synthetic-aisystant-id",
        ),
        patch(
            "clients.aisystant.aisystant.get_subscription_tariffs",
            new_callable=AsyncMock,
            return_value=[tariff],
        ),
        patch(
            "clients.aisystant.aisystant.create_subscription_payment",
            new_callable=AsyncMock,
            return_value={"confirmationUrl": "https://payment.example.invalid/x"},
        ),
        patch(
            "clients.aisystant.aisystant.invalidate_subscription_cache",
            new_callable=MagicMock,
        ),
    ]


_PROFILES: dict[str, Callable[[], list[Any]]] = {
    "linked-participant-with-tariffs": _linked_participant_with_tariffs,
    "new-participant": _new_participant,
    "participant-without-assessment": _participant_without_assessment,
}

_UPDATE_BUILDERS: dict[str, Callable[[str], Update]] = {
    "callback": lambda data: callback_button(data, chat_id=SYNTHETIC_CHAT_ID),
    "command": lambda command: make_command_update(
        command, chat_id=SYNTHETIC_CHAT_ID, user_id=SYNTHETIC_CHAT_ID
    ),
}


# --- replay ------------------------------------------------------------------


def _build_dispatcher() -> Dispatcher:
    """Fresh dispatcher with copies of the scenario routers.

    Copies keep the application's router singletons unattached, so this can
    run in the same process as the smoke suite's shared dispatcher.
    """

    from handlers.diagnose import diagnose_router
    from handlers.onboarding import onboarding_router
    from handlers.subscription import subscription_router

    dispatcher = Dispatcher(storage=MemoryStorage())
    # Same relative order as handlers.setup_handlers().
    for source in (onboarding_router, subscription_router, diagnose_router):
        copy = Router(name=f"behavior-{source.name}")
        copy.message.handlers.extend(source.message.handlers)
        copy.callback_query.handlers.extend(source.callback_query.handlers)
        dispatcher.include_router(copy)
    return dispatcher


async def replay_scenario(scenario: BehaviorScenario) -> list[dict[str, Any]]:
    """Feed the scenario's update through aiogram and return recorded calls."""

    bot = ThinMockBot()
    dispatcher = _build_dispatcher()
    with ExitStack() as stack:
        for patcher in _PROFILES[scenario.profile]():
            stack.enter_context(patcher)
        await dispatcher.feed_update(
            bot, _UPDATE_BUILDERS[scenario.when_kind](scenario.when_value)
        )
    return list(bot.sent)


def _scenario_passed(scenario: BehaviorScenario) -> bool:
    try:
        sent = asyncio.run(replay_scenario(scenario))
    except Exception as exc:  # noqa: BLE001 - a crashing handler fails the scenario
        LOGGER.warning(
            "behavior scenario %s crashed (%s)",
            scenario.scenario_id,
            type(exc).__name__,
        )
        return False
    return response_matches(sent, scenario.expect)


class BotBehaviorScenarioResolver:
    """VerifiedBehaviorScenarioResolverPort replaying scenarios via ThinMockBot.

    `clock` is the caller's TrustedClockPort so `verified_at` is keyed to the
    same clock that `_resolve_behavior_scenarios` checks staleness against.
    """

    def __init__(self, *, clock: TrustedClockPort, spec_path: Path = SPEC_PATH):
        self._clock = clock
        self._spec_path = spec_path

    def resolve_verified(
        self, request: BehaviorScenarioVerificationRequest
    ) -> BehaviorScenarioReceipt:
        scenarios = load_behavior_scenarios(self._spec_path)
        if tuple(s.scenario_id for s in scenarios) != request.scenario_ids:
            raise BehaviorScenarioResolutionError(
                "scenario spec differs from the contract's behavior_scenarios"
            )
        results = tuple(
            BehaviorScenarioResult(
                scenario_id=scenario.scenario_id,
                passed=_scenario_passed(scenario),
                expectation_hash=scenario.spec_hash,
            )
            for scenario in scenarios
        )
        return BehaviorScenarioReceipt(
            schema_version=1,
            receipt_id=str(uuid.uuid4()),
            request_digest=request.request_digest,
            repository=request.repository,
            contract_digest=request.contract_digest,
            contract_edition=request.contract_edition,
            manifest_release_id=request.manifest_release_id,
            artifact_digest=request.artifact_digest,
            scenario_ids=request.scenario_ids,
            results=results,
            verified_at=self._clock.now_utc(),
            verifier_identity=VERIFIER_IDENTITY,
        )
