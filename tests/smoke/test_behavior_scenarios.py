"""
Smoke-тесты: сценарии поведения из контракта выпуска (WP-562 Ф7).

Список сценариев не дублируется здесь — тесты генерируются из единого
источника `.github/release-behavior-scenarios.json`, который читает и
release-резолвер `scripts/release_behavior_scenarios.py`.
"""

import pytest

from scripts.release_behavior_scenarios import (
    load_behavior_scenarios,
    replay_scenario,
    response_matches,
)

SCENARIOS = load_behavior_scenarios()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[scenario.scenario_id for scenario in SCENARIOS]
)
async def test_behavior_scenario_matches_expected_structure(scenario):
    sent = await replay_scenario(scenario)

    assert sent, f"{scenario.scenario_id}: бот ничего не отправил"
    assert response_matches(sent, scenario.expect), (
        f"{scenario.scenario_id}: структура ответа не совпала с ожиданием "
        f"{dict(scenario.expect)}"
    )
