# -*- coding: utf-8 -*-
"""Tests for R5B: vest_only progression ladder uses E = k*body_weight + add-on."""
import sqlite3
import tempfile
import unittest

from koilog import stats, equipment


class TestVestOnlyLadder(unittest.TestCase):
    """Вест-only упражнения используют E-ladder = k*body_weight + a."""

    def test_vest_only_Отжимания_E_ladder_k06(self):
        """Отжимания (k=0.6) с body_weight=70 -> E-ladder [42, 44, ..., 62]."""
        result = stats.available_weights(
            None, "Отжимания", vest=True, body_weight=70.0
        )
        # Ожидание: 0.6 * 70 + [0, 2, 4, ..., 20] = [42, 44, ..., 62]
        expected_start = tuple(round(0.6 * 70 + a, 2) for a in equipment.VEST_LADDER)
        self.assertEqual(result, expected_start)
        self.assertEqual(len(result), len(equipment.VEST_LADDER))
        self.assertEqual(result[0], 42.0)
        self.assertEqual(result[-1], 62.0)

    def test_vest_only_Отжимания_without_bodyweight_returns_empty(self):
        """Без body_weight -> пустой список."""
        result = stats.available_weights(None, "Отжимания", vest=True, body_weight=None)
        self.assertEqual(result, ())

    def test_total_bodyweight_Podyimanie_uses_bodyweight_ladder(self):
        """Подтягивания на перекладине (total_bodyweight) используют тело+добавку."""
        result = stats.available_weights(
            None, "Подтягивания на перекладине", vest=True, body_weight=70.0
        )
        # total_bodyweight использует bodyweight_ladder: тело + VEST_LADDER
        expected = tuple(round(70.0 + a, 2) for a in equipment.VEST_LADDER)
        self.assertEqual(result, expected)
        self.assertEqual(result[0], 70.0)
        self.assertEqual(result[-1], 90.0)

    def test_absolute_dumbbell_unchanged(self):
        """Гантели (absolute) не тронуты."""
        result = stats.available_weights(None, "Жим гантелей лежа", vest=False)
        # DUMBBELL_PAIRS_TOTAL = удвоенная лестница гантелей
        expected = equipment.DUMBBELL_PAIRS_TOTAL
        self.assertEqual(result, expected)

    def test_vest_false_for_pushups_returns_bodyweight_profile(self):
        """vest=False для Отжимания -> profile.bodyweight (пусто)."""
        result = stats.available_weights(None, "Отжимания", vest=False)
        self.assertEqual(result, ())

    def test_regression_non_vest_exercises_untouched(self):
        """Не vest-only упражнения не тронуты."""
        # Планка (bodyweight)
        result = stats.available_weights(None, "Планка", vest=True, body_weight=70.0)
        self.assertEqual(result, ())


if __name__ == "__main__":
    unittest.main()
