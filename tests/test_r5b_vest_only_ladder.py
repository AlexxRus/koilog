# -*- coding: utf-8 -*-
"""Tests for R5B: vest_only progression ladder uses E = k*body_weight + add-on."""
import sqlite3
import unittest

from koilog import stats, equipment, etl, core


def _build_vest_fixture():
    """Синтетическая база для vest-only тестов: Отжимания с разными весами."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(etl.SCHEMA)
    conn.executescript(etl.WEEKLY_KOI_VIEW)
    
    # Отжимания: последние подходы с разными весами для разных сценариев
    # last_weight = 62 (top of ladder), reps = 18
    rows = [
        ("2024-01-10T10:00", "2024-01-10", "Отжимания", 50.0, 15),
        ("2024-01-20T10:00", "2024-01-20", "Отжимания", 55.0, 16),
        ("2024-02-05T10:00", "2024-02-05", "Отжимания", 62.0, 18),  # last_weight
    ]
    
    for i, (session, date, exercise, weight, reps) in enumerate(rows, start=1):
        rm_value = None if weight is None else core.one_rm(weight, reps)
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, equipment, set_index, weight,
                              reps, time_seconds, rm, skipped, is_outlier, is_warmup)
                   VALUES ('journal', ?, ?, ?, ?, 'ГРУДЬ', 'vest', 1, ?, ?, ?, ?, 0, 0, 0)
            """,
            (i, session, date, exercise, weight, reps, 60, rm_value),
        )
    
    conn.commit()
    return conn


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


class TestVestOnlyProgression(unittest.TestCase):
    """Прогрессия для vest-only упражнений — флагманские кейсы."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _build_vest_fixture()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_flagship_progression_starts_at_top_of_E_ladder(self):
        """Флагман (Р1+Р2): Отжимания, тело 70, last_weight=62, reps=18.
        
        - snapped = 62 (верхняя ступень E-ladder)
        - reference_rm = one_rm(62, 18) ≈ 117.47
        - программа стартует с 62
        - next_weight после 62 → None (лестница исчерпана)
        - warning о "не в инвентаре" нет
        """
        plan = stats.progression_from_db(
            self.conn, "Отжимания", vest=True, body_weight=70.0
        )
        
        # Проверяем программу
        self.assertIsNotNone(plan.program)
        self.assertTrue(len(plan.program.steps) > 0)
        
        first_step = plan.program.steps[0]
        self.assertAlmostEqual(first_step.weight, 62.0, places=2)
        self.assertEqual(first_step.reps, 18)
        
        # reference_rm должен быть близок к one_rm(62, 18)
        expected_rm = core.one_rm(62.0, 18)
        actual_rm = first_step.reference_rm
        self.assertAlmostEqual(actual_rm, expected_rm, places=1)
        
        # Warning о "не в инвентаре" не должно быть
        warning_messages = [w for w in plan.warnings if "в инвентаре нет" in str(w).lower()]
        self.assertEqual(len(warning_messages), 0)
        
        # next_weight после 62 → None (лестница исчерпана)
        next_wt = core.next_weight(62.0, stats.available_weights(None, "Отжимания", vest=True, body_weight=70.0), hands=1)
        self.assertIsNone(next_wt)

    def test_below_min_weight_no_program(self):
        """Ниже-min (Р4-A): last_weight = 40 → план не строится, reason содержит ошибку."""
        # Создадим отдельную базу с низким весом
        low_conn = _build_vest_fixture()
        # Перепишем последний подход на низкий вес
        low_conn.execute("UPDATE sets SET weight = 40.0, reps = 12 WHERE date = '2024-02-05'")
        low_conn.execute("UPDATE sets SET rm = ? WHERE date = '2024-02-05'", 
                        (core.one_rm(40.0, 12),))
        low_conn.commit()
        
        try:
            plan = stats.progression_from_db(
                low_conn, "Отжимания", vest=True, body_weight=70.0
            )
            
            # Программа не должна строиться или должна быть пустой
            self.assertIsNotNone(plan.reason)
            self.assertIn("легче самого лёгкого снаряда", plan.reason.lower())
            self.assertIn("42", plan.reason)  # min E-ladder = 42
        finally:
            low_conn.close()

    def test_mid_weight_snaps_floor_with_warning(self):
        """Середина (Р3-A): last_weight = 53 → snapped = 52 (floor), warning."""
        mid_conn = _build_vest_fixture()
        mid_conn.execute("UPDATE sets SET weight = 53.0, reps = 14 WHERE date = '2024-02-05'")
        mid_conn.execute("UPDATE sets SET rm = ? WHERE date = '2024-02-05'",
                        (core.one_rm(53.0, 14),))
        mid_conn.commit()
        
        try:
            plan = stats.progression_from_db(
                mid_conn, "Отжимания", vest=True, body_weight=70.0
            )
            
            # Snap должен быть 52 (nearest floor от 53 в E-ladder)
            first_step = plan.program.steps[0]
            self.assertAlmostEqual(first_step.weight, 52.0, places=2)
            
            # Должно быть предупреждение о snap
            warning_messages = [w for w in plan.warnings if "в инвентаре нет" in w.lower() or "не тяжелее" in w.lower()]
            self.assertGreater(len(warning_messages), 0)
            self.assertIn("53", str(warning_messages))
        finally:
            mid_conn.close()

    def test_vest_exercises_subset_of_pushups_family(self):
        """Инвариант: упражнения с профилем 'vest' ⊆ PUSHUPS_FAMILY."""
        from koilog.etl import PUSHUPS_FAMILY
        
        # Все упражнения с профилем vest должны быть в PUSHUPS_FAMILY
        vest_exercises = [
            name for name, profile_id in equipment.EXERCISE_PROFILE.items()
            if profile_id == "vest"
        ]
        
        for ex in vest_exercises:
            self.assertIn(
                ex, PUSHUPS_FAMILY,
                f"Упражнение '{ex}' имеет профиль vest, но не входит в PUSHUPS_FAMILY"
            )


if __name__ == "__main__":
    unittest.main()
