"""WP-330 C9a — unit tests для routing 4 версий контента.

Покрытие:
  - resolve_variant: 14 кейсов (8 базовых + None/empty/boundary/range)
  - get_day_text: 8 кейсов routing + legacy fallback + aliases
  - calc_words: 5 кейсов (int, str, range, None, malformed)
Итого: 27 кейсов.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from core.marathon_content import resolve_variant, get_day_text  # noqa: E402
from config.settings import calc_words  # noqa: E402


class TestResolveVariant(unittest.TestCase):
    """resolve_variant: 14 кейсов."""

    def test_short_simple_int(self):
        self.assertEqual(resolve_variant(10, 1), "short_simple")

    def test_short_complex_int(self):
        self.assertEqual(resolve_variant(10, 2), "short_complex")

    def test_long_simple_int(self):
        self.assertEqual(resolve_variant(25, 1), "long_simple")

    def test_long_complex_int(self):
        self.assertEqual(resolve_variant(25, 2), "long_complex")

    def test_short_simple_str(self):
        self.assertEqual(resolve_variant("10", "1"), "short_simple")

    def test_long_complex_str(self):
        self.assertEqual(resolve_variant("25", "2"), "long_complex")

    def test_legacy_range_5_10_simple(self):
        self.assertEqual(resolve_variant("5-10", 1), "short_simple")

    def test_legacy_range_15_25_complex_level_3(self):
        self.assertEqual(resolve_variant("15-25", 3), "long_complex")

    def test_boundary_15_complex(self):
        self.assertEqual(resolve_variant("15", 2), "long_complex")

    def test_boundary_14_complex(self):
        self.assertEqual(resolve_variant("14", 2), "short_complex")

    def test_none_duration_none_complexity(self):
        self.assertEqual(resolve_variant(None, None), "long_simple")

    def test_none_duration_level_2(self):
        self.assertEqual(resolve_variant(None, 2), "long_complex")

    def test_empty_strings(self):
        self.assertEqual(resolve_variant("", ""), "long_simple")

    def test_malformed_duration_garbage(self):
        self.assertEqual(resolve_variant("abc", 1), "long_simple")


_FAKE_CONTENT_DAY_1 = {
    "lesson": "legacy lesson D1",
    "practice": "legacy practice D1",
    "checkin": "checkin D1",
    "lesson_full": "long_complex lesson D1 (legacy)",
    "practice_full": "long_complex practice D1 (legacy)",
    "lesson_long_complex": "long_complex lesson D1 (new)",
    "practice_long_complex": "long_complex practice D1 (new)",
    "lesson_long_simple": "long_simple lesson D1",
    "practice_long_simple": "long_simple practice D1",
    "lesson_short_complex": "short_complex lesson D1",
    "practice_short_complex": "short_complex practice D1",
    "lesson_short_simple": "short_simple lesson D1",
    "practice_short_simple": "short_simple practice D1",
    "reflection_question": "Q D1",
    "faq_hint": "FAQ D1",
}


class TestGetDayTextRouting(unittest.TestCase):
    """get_day_text: 8 кейсов routing + 5 fallback/alias/missing."""

    def setUp(self):
        self._patcher = patch("core.marathon_content._CONTENT", {"1": _FAKE_CONTENT_DAY_1})
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_routing_short_simple(self):
        intern = {"study_duration": 10, "complexity_level": 1}
        self.assertEqual(get_day_text(1, "lesson", intern), "short_simple lesson D1")

    def test_routing_short_complex(self):
        intern = {"study_duration": 10, "complexity_level": 2}
        self.assertEqual(get_day_text(1, "practice", intern), "short_complex practice D1")

    def test_routing_long_simple(self):
        intern = {"study_duration": 25, "complexity_level": 1}
        self.assertEqual(get_day_text(1, "lesson", intern), "long_simple lesson D1")

    def test_routing_long_complex(self):
        intern = {"study_duration": 25, "complexity_level": 2}
        self.assertEqual(get_day_text(1, "lesson", intern), "long_complex lesson D1 (new)")

    def test_routing_legacy_range(self):
        intern = {"study_duration": "15-25", "complexity_level": 3}
        self.assertEqual(get_day_text(1, "lesson", intern), "long_complex lesson D1 (new)")

    def test_routing_with_none_complexity(self):
        intern = {"study_duration": 25, "complexity_level": None}
        self.assertEqual(get_day_text(1, "lesson", intern), "long_simple lesson D1")

    def test_no_intern_falls_back_to_legacy_lesson(self):
        self.assertEqual(get_day_text(1, "lesson"), "legacy lesson D1")

    def test_no_intern_falls_back_to_legacy_practice(self):
        self.assertEqual(get_day_text(1, "practice"), "legacy practice D1")

    def test_alias_lesson_full_maps_to_long_complex(self):
        self.assertEqual(get_day_text(1, "lesson_full"), "long_complex lesson D1 (new)")

    def test_alias_practice_full_maps_to_long_complex(self):
        self.assertEqual(get_day_text(1, "practice_full"), "long_complex practice D1 (new)")

    def test_checkin_no_routing(self):
        intern = {"study_duration": 10, "complexity_level": 2}
        self.assertEqual(get_day_text(1, "checkin", intern), "checkin D1")

    def test_missing_day_returns_none(self):
        self.assertIsNone(get_day_text(99, "lesson"))

    def test_missing_variant_falls_back_to_legacy(self):
        partial = {
            "1": {"lesson": "legacy only", "practice": "p"}
        }
        with patch("core.marathon_content._CONTENT", partial):
            intern = {"study_duration": 10, "complexity_level": 1}
            self.assertEqual(get_day_text(1, "lesson", intern), "legacy only")


class TestCalcWords(unittest.TestCase):
    """calc_words: 5 кейсов defensive."""

    def test_int_15_bloom_1(self):
        self.assertEqual(calc_words(15, 1), 900)  # 15*60*1.0

    def test_str_15_bloom_1(self):
        self.assertEqual(calc_words("15", 1), 900)

    def test_legacy_range_5_10(self):
        self.assertEqual(calc_words("5-10", 1), 300)  # split → 5*60*1.0

    def test_legacy_range_15_25(self):
        self.assertEqual(calc_words("15-25", 1), 900)  # split → 15*60*1.0

    def test_none_falls_back_to_15(self):
        self.assertEqual(calc_words(None, 1), 900)

    def test_empty_string_falls_back_to_15(self):
        self.assertEqual(calc_words("", 1), 900)

    def test_malformed_falls_back_to_15(self):
        self.assertEqual(calc_words("abc", 1), 900)

    def test_none_bloom_defaults_to_1(self):
        self.assertEqual(calc_words(15, None), 900)


if __name__ == "__main__":
    unittest.main()
