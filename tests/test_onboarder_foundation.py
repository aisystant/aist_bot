"""Онбордер (WP-406 Ф5) — smoke-тесты core/onboarder/.

Покрытие (без БД — чистые функции и контракт интерфейса):
  - describe_stream: маппинг recommended_stream → человекочитаемое имя (5 кейсов)
  - X3 constants: _RETURN_TO_X3, _RETURN_TO_TTL_SECONDS (инварианты bridge-пути)
  - check_x3_return_to_bridge: существует и принимает правильные аргументы
  - X2_TOPICS: контракт 3 пунктов понимания сообщества (WP-406 Ф21 — «norms» снят)
  - x2._next_topic: выбор следующего неподтверждённого пункта (чистая логика)
  - x2 контент: ориентация и расширение покрывают все пункты X2_TOPICS
  - handle / run_step / confirm_topic / show_more: реализованы как корутины
"""

import inspect
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from core.onboarder import x2, x3, handle  # noqa: E402


class TestDescribeStream(unittest.TestCase):
    """x3.describe_stream: перевод потока в человекочитаемое имя."""

    def test_work_development_stream(self):
        self.assertEqual(x3.describe_stream("РР"), "программа «Рабочее развитие»")

    def test_stage_stream_includes_number_and_code(self):
        result = x3.describe_stream("S2")
        self.assertIn("ступени 2", result)
        self.assertIn("(S2)", result)

    def test_stage_stream_boundary(self):
        self.assertIn("ступени 4", x3.describe_stream("S4"))

    def test_unknown_returned_as_is(self):
        # Не выдумываем имя для незнакомого кода (правило «не изобретать имена»).
        self.assertEqual(x3.describe_stream("Z9"), "Z9")

    def test_empty_returned_as_is(self):
        self.assertEqual(x3.describe_stream(""), "")


class TestX2Topics(unittest.TestCase):
    """x2.X2_TOPICS: контракт трёх пунктов (WP-406 Ф21 — «norms» снят как непользующий)."""

    def test_three_topics(self):
        self.assertEqual(len(x2.X2_TOPICS), 3)

    def test_topics_unique(self):
        self.assertEqual(len(set(x2.X2_TOPICS)), 3)

    def test_topics_cover_community_and_where_to_ask(self):
        self.assertIn("community", x2.X2_TOPICS)
        self.assertIn("where_to_ask", x2.X2_TOPICS)


class TestX3BridgeConstants(unittest.TestCase):
    """X3 bridge-path инварианты — константы и наличие функции (WP-406 Ф5)."""

    def test_return_to_key(self):
        self.assertEqual(x3._RETURN_TO_X3, "x3_offer")

    def test_ttl_is_one_hour(self):
        self.assertEqual(x3._RETURN_TO_TTL_SECONDS, 3600)

    def test_check_x3_return_to_bridge_is_callable(self):
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(x3.check_x3_return_to_bridge))

    def test_show_x3_offer_is_callable(self):
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(x3._show_x3_offer))


class TestX2NextTopic(unittest.TestCase):
    """x2._next_topic: первый неподтверждённый пункт в порядке X2_TOPICS."""

    def test_empty_confirmed_returns_first(self):
        self.assertEqual(x2._next_topic([]), x2.X2_TOPICS[0])

    def test_skips_confirmed_in_order(self):
        # Подтверждён только первый → следующий = второй.
        self.assertEqual(x2._next_topic([x2.X2_TOPICS[0]]), x2.X2_TOPICS[1])

    def test_out_of_order_confirmed(self):
        # Подтверждён второй, но не первый → возвращает первый (порядок X2_TOPICS).
        self.assertEqual(x2._next_topic([x2.X2_TOPICS[1]]), x2.X2_TOPICS[0])

    def test_all_confirmed_returns_none(self):
        self.assertIsNone(x2._next_topic(list(x2.X2_TOPICS)))


class TestX2Content(unittest.TestCase):
    """Контент Х2 покрывает все пункты X2_TOPICS — иначе шаг покажет KeyError."""

    def test_topic_text_covers_all_topics(self):
        self.assertEqual(set(x2._TOPIC_TEXT.keys()), set(x2.X2_TOPICS))

    def test_topic_more_covers_all_topics(self):
        self.assertEqual(set(x2._TOPIC_MORE.keys()), set(x2.X2_TOPICS))


class TestOnboarderEntrypointsAreCoroutines(unittest.TestCase):
    """Вход Онбордера и срез Х2 реализованы как корутины (не заглушки)."""

    def test_handle_is_coroutine(self):
        self.assertTrue(inspect.iscoroutinefunction(handle))

    def test_run_step_is_coroutine(self):
        self.assertTrue(inspect.iscoroutinefunction(x2.run_step))

    def test_confirm_topic_is_coroutine(self):
        self.assertTrue(inspect.iscoroutinefunction(x2.confirm_topic))

    def test_show_more_is_coroutine(self):
        self.assertTrue(inspect.iscoroutinefunction(x2.show_more))

    def test_should_handle_removed(self):
        # should_handle удалён (explicit-вход, не перехват) — его не должно быть в модуле.
        import core.onboarder as onb
        self.assertFalse(hasattr(onb, "should_handle"))


class TestOfferCooldown(unittest.TestCase):
    """offer._on_cooldown: мягкий повтор оффера «Освоиться» не чаще окна cooldown."""

    def setUp(self):
        from core.onboarder import offer
        self._fn = offer._on_cooldown
        self._window = offer._COOLDOWN_DAYS

    def test_never_offered_not_on_cooldown(self):
        self.assertFalse(self._fn(None))
        self.assertFalse(self._fn(""))

    def test_recent_offer_on_cooldown(self):
        from datetime import datetime, timedelta
        recent = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        self.assertTrue(self._fn(recent))

    def test_old_offer_not_on_cooldown(self):
        from datetime import datetime, timedelta
        old = (datetime.utcnow() - timedelta(days=self._window + 1)).isoformat()
        self.assertFalse(self._fn(old))

    def test_broken_timestamp_not_on_cooldown(self):
        # Битый timestamp не должен навсегда заблокировать оффер.
        self.assertFalse(self._fn("not-a-date"))


class TestOfferJustShown(unittest.TestCase):
    """offer._just_shown: короткое дедуп-окно (независимо от 3-дневного cooldown).

    Peer-сессия 2026-08-12-12 (WP-406) — cross-channel дубль между /start
    (bypass_cooldown=True) и Проводником (handlers/hermes.py, обычный cooldown),
    найдено code review при фиксе.
    """

    def setUp(self):
        from core.onboarder import offer
        self._fn = offer._just_shown
        self._window = offer._DEDUP_SECONDS

    def test_never_offered_not_just_shown(self):
        self.assertFalse(self._fn(None))
        self.assertFalse(self._fn(""))

    def test_offered_seconds_ago_is_just_shown(self):
        from datetime import datetime, timedelta
        recent = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
        self.assertTrue(self._fn(recent))

    def test_offered_past_dedup_window_not_just_shown(self):
        from datetime import datetime, timedelta
        past_window = (datetime.utcnow() - timedelta(seconds=self._window + 5)).isoformat()
        self.assertFalse(self._fn(past_window))

    def test_broken_timestamp_not_just_shown(self):
        self.assertFalse(self._fn("not-a-date"))


class TestShouldOfferBypassCooldown(unittest.IsolatedAsyncioTestCase):
    """offer.should_offer(bypass_cooldown=...): /start игнорирует 3-дневный cooldown,
    но не игнорирует короткое дедуп-окно (не показывает дубль сразу после Проводника).
    """

    def _patch_storage(self, mock, has_gap: bool, offered_at):
        from unittest.mock import AsyncMock, patch

        status = {"x2_done": not has_gap, "x3_done": not has_gap}
        ctx = {"offer_shown_at": offered_at} if offered_at else {}
        return patch.multiple(
            "core.onboarder.storage",
            get_status=AsyncMock(return_value=status),
            get_onboarding_context=AsyncMock(return_value=ctx),
        )

    async def test_bypass_ignores_3day_cooldown(self):
        from datetime import datetime, timedelta
        from core.onboarder import offer

        recent = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        with self._patch_storage(self, has_gap=True, offered_at=recent):
            self.assertFalse(await offer.should_offer(123, bypass_cooldown=False))
            self.assertTrue(await offer.should_offer(123, bypass_cooldown=True))

    async def test_bypass_still_respects_dedup_window(self):
        from datetime import datetime, timedelta
        from core.onboarder import offer

        just_now = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
        with self._patch_storage(self, has_gap=True, offered_at=just_now):
            self.assertFalse(await offer.should_offer(123, bypass_cooldown=True))

    async def test_no_gap_never_offers_even_with_bypass(self):
        from core.onboarder import offer

        with self._patch_storage(self, has_gap=False, offered_at=None):
            self.assertFalse(await offer.should_offer(123, bypass_cooldown=True))


class TestOfferPayload(unittest.TestCase):
    """offer.offer_payload: единый источник текста и кнопки для всех точек входа."""

    def test_payload_has_required_keys(self):
        from core.onboarder import offer
        p = offer.offer_payload()
        self.assertEqual(set(p.keys()), {"text", "button_text", "callback_data"})

    def test_callback_routes_to_onboarder_entry(self):
        from core.onboarder import offer
        # callback_data должен совпадать с фильтром входа on_onboarder_start.
        self.assertEqual(offer.offer_payload()["callback_data"], "onboarder_start")

    def test_should_offer_is_coroutine(self):
        from core.onboarder import offer
        self.assertTrue(inspect.iscoroutinefunction(offer.should_offer))


class TestHermesOnboardingQuery(unittest.TestCase):
    """hermes._is_onboarding_query: кнопка «Освоиться» только на вопросы про вход."""

    def setUp(self):
        from handlers.hermes import _is_onboarding_query
        self._fn = _is_onboarding_query

    def test_onboarding_phrases_match(self):
        for q in ("как тут всё устроено?", "с чего начать?", "хочу освоиться",
                  "какой первый шаг", "что здесь происходит"):
            self.assertTrue(self._fn(q), q)

    def test_specific_question_does_not_match(self):
        # Частный вопрос — кнопку не навязываем.
        for q in ("какой сегодня день марафона?", "сколько уроков осталось",
                  "когда придёт следующий урок"):
            self.assertFalse(self._fn(q), q)

    def test_empty_does_not_match(self):
        self.assertFalse(self._fn(""))
        self.assertFalse(self._fn(None))


if __name__ == "__main__":
    unittest.main()
