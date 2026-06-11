"""Фундамент Онбордера (WP-406 Ф5) — smoke-тесты каркаса core/onboarder/.

Покрытие (без БД — чистые функции и контракт интерфейса):
  - describe_stream: маппинг recommended_stream → человекочитаемое имя (5 кейсов)
  - X2_TOPICS: контракт 4 пунктов понимания сообщества
  - заглушки интерфейса (should_handle/handle/run_step/run_x3) явно поднимают
    NotImplementedError — «Фундамент» не подключён к живому роутингу
"""

import asyncio
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from core.onboarder import x2, x3, should_handle, handle  # noqa: E402


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


class TestInterfaceStubsRaise(unittest.TestCase):
    """Заглушки интерфейса поднимают NotImplementedError (не молчат, не возвращают None)."""

    def test_should_handle_raises(self):
        with self.assertRaises(NotImplementedError):
            asyncio.run(should_handle({}, None))

    def test_handle_raises(self):
        with self.assertRaises(NotImplementedError):
            asyncio.run(handle({}, None))

    def test_x2_run_step_raises(self):
        with self.assertRaises(NotImplementedError):
            asyncio.run(x2.run_step({}, None))

    def test_x3_run_x3_raises(self):
        with self.assertRaises(NotImplementedError):
            asyncio.run(x3.run_x3({}, None))


if __name__ == "__main__":
    unittest.main()
