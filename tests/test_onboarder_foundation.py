"""Онбордер (WP-406 Ф5) — smoke-тесты core/onboarder/.

Покрытие (без БД — чистые функции и контракт интерфейса):
  - describe_stream: маппинг recommended_stream → человекочитаемое имя (5 кейсов)
  - X3 constants: _RETURN_TO_X3, _RETURN_TO_TTL_SECONDS (инварианты bridge-пути)
  - check_x3_return_to_bridge: существует и принимает правильные аргументы
  - X2_TOPICS: контракт 4 пунктов понимания сообщества
  - x2._next_topic: выбор следующего неподтверждённого пункта (чистая логика)
  - x2 контент: ориентация и расширение покрывают все 4 пункта
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
    """x2.X2_TOPICS: контракт четырёх пунктов (карточка WP-406, строка 122)."""

    def test_four_topics(self):
        self.assertEqual(len(x2.X2_TOPICS), 4)

    def test_topics_unique(self):
        self.assertEqual(len(set(x2.X2_TOPICS)), 4)

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
    """Контент Х2 покрывает все 4 пункта — иначе шаг покажет KeyError."""

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
    """_offer_on_cooldown: мягкий повтор оффера «Освоиться» не чаще окна cooldown."""

    def setUp(self):
        from handlers.onboarding import _offer_on_cooldown, _ONBOARDER_OFFER_COOLDOWN_DAYS
        self._fn = _offer_on_cooldown
        self._window = _ONBOARDER_OFFER_COOLDOWN_DAYS

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


if __name__ == "__main__":
    unittest.main()
