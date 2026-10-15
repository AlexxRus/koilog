"""Тесты koilog.stats: статистика по упражнениям и программа из базы.

Фикстура - маленькая синтетическая база, собранная вручную, чтобы
результат каждой проверки можно было посчитать на бумаге. Реальные данные
проверяются в tests.test_etl.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from koilog import equipment, etl, stats
from koilog.core import next_weight, one_rm


#: Подходы, из которых строится фикстура.
#:
#: ``Жим гантелей лежа`` - три тренировки, 1RM растёт 40 -> 43.2 -> 45.
#: ``Скручивания`` - две тренировки, 1RM падает. ``Планка`` - работа на
#: время, 1RM не считается вовсе.
ROWS = [
    # (session, date, exercise, weight, reps)
    ("2024-01-10T10:00", "2024-01-10", "Жим гантелей лежа", 30.0, 10),
    ("2024-01-10T10:00", "2024-01-10", "Жим гантелей лежа", 30.0, 8),
    ("2024-01-10T10:00", "2024-01-10", "Скручивания", 20.0, 12),
    ("2024-01-20T10:00", "2024-01-20", "Жим гантелей лежа", 32.0, 10),
    ("2024-01-20T10:00", "2024-01-20", "Скручивания", 18.0, 12),
    ("2024-02-05T10:00", "2024-02-05", "Жим гантелей лежа", 34.0, 10),
    ("2024-02-05T10:00", "2024-02-05", "Планка", None, None),
]


def _build() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(etl.SCHEMA)
    conn.executescript(etl.WEEKLY_KOI_VIEW)
    for i, (session, date, exercise, weight, reps) in enumerate(ROWS, start=1):
        group = "ЖИМ ЛЁЖА"
        if exercise == "Скручивания":
            group = "ПРЕСС"
        elif exercise == "Планка":
            group = "ПЛАНКА"
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, equipment, set_index, weight,
                              reps, time_seconds, rm, skipped)
                 VALUES ('journal', ?, ?, ?, ?, ?, 'гантели', 1, ?, ?, ?, ?, 0)
            """,
            (i, session, date, exercise, group, weight, reps,
             60 if weight is None else None,
             None if weight is None else one_rm(weight, reps)),
        )
    conn.commit()
    return conn


class ExerciseReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = _build()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def one(self, name: str) -> stats.ExerciseStats:
        found = stats.exercise_report(self.conn, exercise=name)
        self.assertEqual(len(found), 1, name)
        return found[0]

    # -- агрегаты ---------------------------------------------------------

    def test_counts_sessions_and_sets(self):
        row = self.one("Жим гантелей лежа")
        # 30x10 и 30x8 в одной тренировке, плюс две отдельные
        self.assertEqual(row.sets, 4)
        self.assertEqual(row.sessions, 3)
        self.assertEqual(row.reps_total, 38)

    def test_volume_sums_weight_times_reps(self):
        row = self.one("Жим гантелей лежа")
        self.assertAlmostEqual(row.volume, 30 * 10 + 30 * 8 + 32 * 10 + 34 * 10)

    def test_date_span(self):
        row = self.one("Жим гантелей лежа")
        self.assertEqual(row.first_day, "2024-01-10")
        self.assertEqual(row.last_day, "2024-02-05")

    def test_group_and_equipment_come_from_the_row(self):
        self.assertEqual(self.one("Скручивания").exercise_group, "ПРЕСС")
        self.assertEqual(self.one("Жим гантелей лежа").equipment, "гантели")

    # -- лучший и последний ----------------------------------------------

    def test_best_is_highest_rm_not_latest(self):
        row = self.one("Жим гантелей лежа")
        # 30x10 -> 40.0, 30x8 -> 34.3, 32x10 -> 42.7, 34x10 -> 45.4
        self.assertAlmostEqual(row.best_rm, one_rm(34.0, 10))
        self.assertEqual(row.best_day, "2024-02-05")
        self.assertEqual(row.best_weight, 34.0)
        self.assertEqual(row.best_reps, 10)

    def test_best_uses_the_set_that_produced_it(self):
        """Вернуться должны вес и повторы именно того подхода, а не последние."""
        row = self.one("Скручивания")
        self.assertEqual(row.best_weight, 20.0)
        self.assertEqual(row.best_reps, 12)
        # а последний подход - более лёгкий
        self.assertEqual(row.last_weight, 18.0)

    def test_last_is_most_recent_session(self):
        row = self.one("Жим гантелей лежа")
        self.assertEqual(row.last_day_rm, "2024-02-05")
        self.assertAlmostEqual(row.last_rm, one_rm(34.0, 10))

    def test_delta_is_last_minus_previous(self):
        row = self.one("Жим гантелей лежа")
        self.assertAlmostEqual(
            row.last_delta, one_rm(34.0, 10) - one_rm(32.0, 10)
        )
        self.assertIsNotNone(row.is_going_up)
        self.assertTrue(row.is_going_up)

    def test_delta_is_negative_when_falling(self):
        row = self.one("Скручивания")
        self.assertLess(row.last_delta, 0)
        self.assertFalse(row.is_going_up)

    def test_single_session_has_no_delta(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(etl.SCHEMA)
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, set_index, weight, reps, rm)
                 VALUES ('journal', 1, 's1', '2024-01-01', 'Жим ногами',
                         'ПРИСЕДАНИЯ', 1, 100.0, 5, 115.4)
            """
        )
        row = stats.exercise_report(conn, exercise="Жим ногами")[0]
        self.assertIsNone(row.last_delta)
        self.assertIsNone(row.is_going_up)
        conn.close()

    def test_timed_exercise_has_no_rm(self):
        row = self.one("Планка")
        self.assertEqual(row.sets, 1)
        self.assertIsNone(row.best_rm)
        self.assertIsNone(row.last_rm)
        self.assertEqual(row.best_set, "нет данных")
        self.assertEqual(row.last_set, "нет данных")

    def test_best_set_is_readable(self):
        self.assertEqual(
            self.one("Жим гантелей лежа").best_set,
            f"34 кг x 10 -> {one_rm(34.0, 10):.1f}",
        )

    # -- фильтры и сортировка --------------------------------------------

    def test_with_rm_only_drops_timed(self):
        names = {r.exercise for r in stats.exercise_report(self.conn)}
        self.assertIn("Планка", names)
        kept = {r.exercise for r in stats.exercise_report(self.conn, with_rm_only=True)}
        self.assertNotIn("Планка", kept)
        self.assertIn("Скручивания", kept)

    def test_min_sets(self):
        names = {
            r.exercise
            for r in stats.exercise_report(self.conn, min_sets=2)
        }
        self.assertEqual(names, {"Жим гантелей лежа", "Скручивания"})

    def test_filter_by_group(self):
        names = {r.exercise for r in stats.exercise_report(self.conn, group="ПРЕСС")}
        self.assertEqual(names, {"Скручивания"})

    def test_unknown_exercise_matches_nothing(self):
        self.assertEqual(stats.exercise_report(self.conn, exercise="Нет"), [])

    def test_order_by_best_rm(self):
        order = [r.exercise for r in stats.exercise_report(self.conn, order="best_rm")]
        self.assertEqual(order[0], "Жим гантелей лежа")
        self.assertEqual(order[-1], "Планка")

    def test_order_by_last_delta(self):
        """По убыванию: сначала рост, потом падение, потом без дельты."""
        order = [r.exercise for r in stats.exercise_report(self.conn, order="last_delta")]
        # жим +2.7, скручивания -2.3, планка без дельты вовсе
        self.assertEqual(order, ["Жим гантелей лежа", "Скручивания", "Планка"])

    def test_order_by_name(self):
        order = [r.exercise for r in stats.exercise_report(self.conn, order="exercise")]
        self.assertEqual(order, sorted(order, key=str.casefold))

    def test_order_by_sets(self):
        order = [r.exercise for r in stats.exercise_report(self.conn, order="sets")]
        self.assertEqual(order[0], "Жим гантелей лежа")

    def test_limit(self):
        self.assertEqual(len(stats.exercise_report(self.conn, limit=2)), 2)

    def test_unknown_order_is_rejected(self):
        with self.assertRaises(ValueError):
            stats.exercise_report(self.conn, order="вес")

    def test_format_report_mentions_every_exercise(self):
        text = stats.format_report(stats.exercise_report(self.conn))
        for name in ("Жим гантелей лежа", "Скручивания", "Планка"):
            self.assertIn(name, text)

    def test_format_report_on_empty(self):
        self.assertIn("нет упражнений", stats.format_report([]))

    # -- окно по дате -----------------------------------------------------

    def test_since_keeps_only_rows_in_the_window(self):
        rows = {
            r.exercise: r
            for r in stats.exercise_report(self.conn, since="2024-01-15")
        }
        # до 15 января в окно не попадают: у «Жима» остаются две
        # тренировки (20.01 и 05.02), у «Скручиваний» - одна
        self.assertEqual(rows["Жим гантелей лежа"].sets, 2)
        self.assertEqual(rows["Скручивания"].sets, 1)

    def test_since_drops_exercises_absent_from_the_window(self):
        """Смысл окна: упражнение, которым не занимались, не мешает."""
        names = {
            r.exercise
            for r in stats.exercise_report(self.conn, since="2024-02-01")
        }
        self.assertEqual(names, {"Жим гантелей лежа", "Планка"})

    def test_since_still_measures_delta_from_before_the_window(self):
        """Дельта последней тренировки - от предыдущей, даже если та за
        границей окна: иначе первая тренировка окна всегда показывала бы
        ``None``."""
        row = [
            r for r in stats.exercise_report(self.conn, since="2024-02-01")
            if r.exercise == "Жим гантелей лежа"
        ][0]
        previous = one_rm(32.0, 10)      # 20.01 - уже за границей окна
        last = one_rm(34.0, 10)          # 05.02 - последняя в окне
        self.assertEqual(row.last_day, "2024-02-05")
        self.assertAlmostEqual(row.last_delta, last - previous)
        self.assertTrue(row.is_going_up)

    def test_since_narrows_the_best_rm(self):
        """Лучший 1RM выбирается внутри окна."""
        full = next(
            r for r in stats.exercise_report(self.conn)
            if r.exercise == "Скручивания"
        )
        windowed = next(
            r for r in stats.exercise_report(self.conn, since="2024-01-15")
            if r.exercise == "Скручивания"
        )
        self.assertGreater(full.best_rm, windowed.best_rm)

    def test_since_filters_totals_too(self):
        """Объём и первая дата считаются по окну."""
        row = next(
            r for r in stats.exercise_report(self.conn, since="2024-01-15")
            if r.exercise == "Жим гантелей лежа"
        )
        self.assertEqual(row.first_day, "2024-01-20")
        self.assertAlmostEqual(row.volume, 32.0 * 10 + 34.0 * 10)

    def test_without_since_everything_is_shown(self):
        names = {r.exercise for r in stats.exercise_report(self.conn)}
        self.assertEqual(names, {"Жим гантелей лежа", "Скручивания", "Планка"})

    def test_since_for_months_counts_back_from_the_last_workout(self):
        self.assertEqual(stats.since_for_months(self.conn, 3), "2023-11-05")
        self.assertEqual(stats.since_for_months(self.conn, 1), "2024-01-05")
        self.assertIsNone(stats.since_for_months(self.conn, 0))


class AvailableWeightsTests(unittest.TestCase):
    """Список весов берётся из инвентаря, а не из истории.

    Раньше эти тесты проверяли запрос ``SELECT DISTINCT weight``, и весь
    класс осмысленно оправдывался длинным объяснением в докстринге: мол,
    список из базы уже в суммарных килограммах, поэтому ``hands`` должен
    быть 1. Теперь источник один - :mod:`koilog.equipment`, и проверять надо
    именно то, что он не путает единицы и не подставляет чужой вес.
    """

    def setUp(self):
        self.conn = _build()

    def tearDown(self):
        self.conn.close()

    def test_comes_from_inventory_not_from_history(self):
        """В базе записано 30/32/34, а инвентарь даёт 10…80."""
        weights = stats.available_weights(self.conn, "Жим гантелей лежа")
        self.assertEqual(weights, equipment.available_weights("Жим гантелей лежа"))
        self.assertIn(80.0, weights)
        self.assertNotIn(32.0, weights)

    def test_two_hand_dumbbell_is_in_total_weight(self):
        weights = stats.available_weights(self.conn, "Жим гантелей лежа")
        self.assertEqual(weights[0], 10.0)   # 2 x 5 кг
        self.assertEqual(weights[-1], 80.0)  # 2 x 40 кг
        self.assertEqual(len(weights), 17)

    def test_one_hand_dumbbell_is_in_single_weight(self):
        weights = stats.available_weights(self.conn, "Махи в наклоне одной рукой")
        self.assertEqual(weights, equipment.DUMBBELL_PAIRS)
        self.assertEqual(weights[0], 5.0)

    def test_bodyweight_exercise_has_no_weights(self):
        self.assertEqual(stats.available_weights(self.conn, "Планка"), ())

    def test_vest_is_off_by_default_and_on_with_flag(self):
        self.assertEqual(stats.available_weights(self.conn, "Отжимания"), ())
        with_vest = stats.available_weights(self.conn, "Отжимания", vest=True)
        self.assertEqual(with_vest[0], 0.0)
        self.assertEqual(with_vest[-1], 20.0)
        self.assertEqual(len(with_vest), 11)

    def test_bodyweight_ladder_needs_body_weight(self):
        exercise = "Подтягивания на перекладине"
        self.assertEqual(stats.available_weights(self.conn, exercise), ())
        ladder = stats.available_weights(
            self.conn, exercise, vest=True, body_weight=68.0
        )
        self.assertEqual(ladder[0], 68.0)
        self.assertEqual(ladder[-1], 88.0)

    def test_min_weight_filter(self):
        self.assertEqual(
            stats.available_weights(
                self.conn, "Жим гантелей лежа", min_weight=31.0
            )[:3],
            (36.0, 40.0, 44.0),
        )

    def test_every_taxonomy_exercise_has_a_profile(self):
        self.assertEqual(equipment.unmapped_exercises(), [])


class ProgressionFromDbTests(unittest.TestCase):
    def setUp(self):
        self.conn = _build()

    def tearDown(self):
        self.conn.close()

    def test_starts_from_the_nearest_stock_weight(self):
        """Записано 34 кг, в инвентаре их нет - старт с 30 (2 x 15).

        Приведение идёт только вниз: предложить 36 значит предложить
        снаряд тяжелее, чем последний выигранный подход, и это уже
        пересказ чужого веса, а не продолжение истории.
        """
        plan = stats.progression_from_db(self.conn, "Жим гантелей лежа")
        self.assertEqual(plan.program.steps[0].sets[0].weight, 30.0)
        self.assertEqual(plan.program.steps[0].sets[0].reps, 10)

    def test_mismatch_with_inventory_is_reported_not_hidden(self):
        plan = stats.progression_from_db(self.conn, "Жим гантелей лежа")
        self.assertTrue(
            any("в инвентаре нет" in w for w in plan.warnings),
            plan.warnings,
        )

    def test_available_comes_from_inventory(self):
        plan = stats.progression_from_db(self.conn, "Жим гантелей лежа")
        self.assertEqual(plan.available, equipment.DUMBBELL_PAIRS_TOTAL)
        self.assertEqual(plan.profile, "dumbbell_2")

    def test_hands_is_two_for_two_hand_dumbbells(self):
        """Справочное поле: вес делится на две руки."""
        plan = stats.progression_from_db(self.conn, "Жим гантелей лежа")
        self.assertEqual(plan.hands, 2)

    def test_next_weight_agrees_with_the_ladder(self):
        """Каждая смена веса - ровно следующий снаряд в инвентаре.

        Шаги программы повторяют вес, пока растут повторы, поэтому сравнивать
        надо только смены: иначе проверка натыкается на «30 -> 30» и падает
        не потому, что расчёт неверен.
        """
        plan = stats.progression_from_db(self.conn, "Жим гантелей лежа")
        changes: list[float] = []
        for step in plan.program:
            weight = step.sets[0].weight
            if not changes or changes[-1] != weight:
                changes.append(weight)
        self.assertGreaterEqual(len(changes), 2)
        for previous, following in zip(changes, changes[1:]):
            self.assertEqual(following, next_weight(previous, plan.available, 1))

    def test_program_only_offers_stock_weights(self):
        plan = stats.progression_from_db(self.conn, "Жим гантелей лежа")
        for step in plan.program:
            for spec in step.sets:
                self.assertIn(spec.weight, plan.available)

    def test_exhausted_list_is_explained(self):
        """Самый тяжёлый снаряд инвентаря - расти некуда."""
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", start_weight=80.0, start_reps=10
        )
        self.assertIsNotNone(plan.reason)
        self.assertIn("верх списка снарядов", plan.reason)

    def test_transition_plan_is_built_too(self):
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", start_weight=30.0, start_reps=8
        )
        self.assertTrue(len(plan.wave) >= 1)
        step = plan.wave[0]
        self.assertAlmostEqual(step.delta, step.rm - step.reference_rm)

    def test_transitions_start_at_six_reps_by_default(self):
        """Волна со сбросом: каждый новый вес - ровно 6 повторов."""
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", start_weight=30.0, start_reps=8
        )
        for step in plan.wave:
            self.assertEqual(step.reps, 6)

    def test_optimal_reps_is_opt_in(self):
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", start_weight=30.0, start_reps=8,
            optimal_reps=True,
        )
        self.assertTrue(plan.wave)
        self.assertTrue(any(step.reps != 6 for step in plan.wave))

    def test_start_can_be_overridden(self):
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", start_weight=30.0, start_reps=5
        )
        self.assertEqual(plan.program.steps[0].sets[0].weight, 30.0)
        self.assertEqual(plan.program.steps[0].sets[0].reps, 5)

    def test_bodyweight_exercise_has_no_program_and_says_so(self):
        plan = stats.progression_from_db(self.conn, "Планка")
        self.assertEqual(plan.available, ())
        self.assertEqual(len(plan.program), 0)
        self.assertIsNotNone(plan.reason)

    def test_explicit_available_is_respected(self):
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", available=(30.0, 40.0, 50.0)
        )
        self.assertEqual(plan.available, (30.0, 40.0, 50.0))

    def test_use_db_reference_takes_the_rolling_average(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(etl.SCHEMA)
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, set_index, weight, reps, rm,
                              rm_ma_30d)
                 VALUES ('journal', 1, 's1', '2024-01-01', 'Жим ногами',
                         'ПРИСЕДАНИЯ', 1, 100.0, 5, ?, 150.0)
            """,
            (one_rm(100.0, 5),),
        )
        plan = stats.progression_from_db(conn, "Жим ногами", use_db_reference=True)
        self.assertAlmostEqual(plan.reference_rm, 150.0)
        # без флага база - собственная 1RM стартового подхода,
        # пересчитанная из веса и повторов, а не взятая из колонки rm
        plan = stats.progression_from_db(conn, "Жим ногами")
        self.assertAlmostEqual(plan.reference_rm, one_rm(100.0, 5))
        conn.close()

    # -- случаи без данных ------------------------------------------------

    def test_missing_exercise_returns_a_reason_not_an_error(self):
        plan = stats.progression_from_db(self.conn, "Такого нет")
        self.assertIn("не найдено", plan.reason)
        self.assertEqual(len(plan.program), 0)
        self.assertEqual(plan.stats.sets, 0)

    def test_missing_exercise_keeps_the_name_readable(self):
        plan = stats.progression_from_db(self.conn, "Такого нет")
        self.assertEqual(plan.stats.exercise, "Такого нет")

    def test_timed_exercise_has_nothing_to_build_from(self):
        plan = stats.progression_from_db(self.conn, "Планка")
        self.assertIn("1RM", plan.reason)
        self.assertEqual(len(plan.program), 0)

    def test_plan_is_iterable_as_the_program(self):
        plan = stats.progression_from_db(self.conn, "Жим гантелей лежа")
        self.assertEqual(len(list(plan)), len(plan.program))

    def test_format_plan_mentions_the_exercise(self):
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", start_weight=30.0, start_reps=8
        )
        text = stats.format_plan(plan)
        self.assertIn("Жим гантелей лежа", text)
        self.assertIn("лучший 1RM", text)
        # Переходы вынесены в format_transitions - в программе их быть не должно.
        self.assertNotIn("переходы на следующий вес", text)

    def test_format_transitions_are_separate(self):
        plan = stats.progression_from_db(
            self.conn, "Жим гантелей лежа", start_weight=30.0, start_reps=8
        )
        text = stats.format_transitions(plan.wave)
        self.assertIn("переходы на следующий вес", text)

    def test_format_transitions_empty(self):
        self.assertEqual(stats.format_transitions(()), "")

    def test_format_plan_shows_the_reason(self):
        plan = stats.progression_from_db(self.conn, "Такого нет")
        self.assertIn("не найдено", stats.format_plan(plan))


class ProgressionFromAvailableTests(unittest.TestCase):
    """Обёртка для случая, когда базы нет, а список гантелей известен."""

    def test_returns_program_and_transitions(self):
        dumbbells = [5, 7, 9, 11, 13, 15, 18, 20, 22, 25, 27, 29, 32, 34, 36, 38, 40]
        program, wave = stats.progression_from_available(
            dumbbells, 22.0, 11.0, hands=2
        )
        self.assertTrue(len(program) > 0)
        # 22 кг суммарных = 11 на руку, 1RM = 30.19
        self.assertAlmostEqual(program.steps[0].reference_rm, one_rm(22.0, 11))
        self.assertTrue(len(wave) > 0)

    def test_reference_rm_can_be_forced(self):
        program, _ = stats.progression_from_available(
            [10.0, 20.0], 10.0, 10.0, reference_rm=12.0
        )
        self.assertAlmostEqual(program.steps[0].reference_rm, 12.0)


class WeeklyKoiTests(unittest.TestCase):
    def setUp(self):
        self.conn = _build()
        # проставляем КОИ вручную: формула тут не проверяется, а view -
        # тем более.
        for i, value in enumerate(
            [1.0, 0.5, 0.5, 1.5, 0.5, 3.5, None], start=1
        ):
            self.conn.execute(
                "UPDATE sets SET koi = ? WHERE source_row = ?", (value, i)
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_reads_the_view(self):
        weeks = stats.weekly_koi(self.conn)
        self.assertTrue(weeks)
        self.assertEqual(
            {w.exercise_group for w in weeks}, {"ЖИМ ЛЁЖА", "ПРЕСС"}
        )

    def test_group_filter(self):
        weeks = stats.weekly_koi(self.conn, group="ПРЕСС")
        self.assertTrue(weeks)
        self.assertEqual({w.exercise_group for w in weeks}, {"ПРЕСС"})

    def test_sets_without_koi_are_excluded(self):
        """``Планка`` - работа на время, КОИ у неё NULL."""
        groups = {w.exercise_group for w in stats.weekly_koi(self.conn)}
        self.assertNotIn("ПЛАНКА", groups)

    def test_band_comes_from_the_view(self):
        row = max(stats.weekly_koi(self.conn), key=lambda w: w.koi_week)
        self.assertIn(row.band, {"Лёгкий", "Тяжёлый", "Очень тяжёлый",
                                 "Вы сошли с ума??"})

    def test_koi_per_set_is_gone(self):
        """Колонку «на подход» убрали: полоса задана для недельной суммы."""
        row = stats.weekly_koi(self.conn)[0]
        self.assertFalse(hasattr(row, "koi_per_set"))

    def test_format_weekly_koi_has_headers(self):
        text = stats.format_weekly_koi(stats.weekly_koi(self.conn))
        self.assertIn("неделя", text)
        self.assertIn("упражнение", text)
        self.assertIn("режим", text)
        self.assertNotIn("на подход", text)

    def test_format_weekly_koi_empty(self):
        self.assertIn("нет", stats.format_weekly_koi([]))


def _build_history() -> sqlite3.Connection:
    """База с историей, покрывающей все четыре окна.

    Подходы расставлены так, чтобы каждое окно видело разный объём:
    месяц - два подхода по 100, квартал - ещё три по 50, год - ещё четыре по
    25. Всё это считается на глаз, никаких календарных фокусов: даты берутся
    из ``stats._shift``, чтобы окна считались одинаково и на тесте, и в коде.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(etl.SCHEMA)

    until = "2024-06-30"
    plan = [
        # (сколько дней назад, вес, повторов)
        (5, 100.0, 10), (10, 100.0, 10),
        (40, 50.0, 10), (50, 50.0, 10), (60, 50.0, 10),
        (100, 25.0, 10), (150, 25.0, 10), (200, 25.0, 10), (300, 25.0, 10),
    ]
    for i, (days, weight, reps) in enumerate(plan, start=1):
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, equipment, set_index, weight,
                              reps, rm, skipped, koi)
             VALUES ('journal', ?, ?, ?, 'Жим штанги лежа', 'ЖИМ ЛЁЖА',
                     'штанга', 1, ?, ?, ?, 0, 0.2)
            """,
            (i, f"s{i}", stats._shift(until, days), weight, reps,
             one_rm(weight, reps)),
        )
    conn.commit()
    return conn


class PeriodTests(unittest.TestCase):
    def setUp(self):
        self.conn = _build_history()

    def tearDown(self):
        self.conn.close()

    def test_four_windows_with_names(self):
        rows = stats.periods(self.conn, "Жим штанги лежа")
        self.assertEqual([r.days for r in rows], list(stats.PERIOD_DAYS))
        self.assertEqual(
            [r.name for r in rows], ["месяц", "квартал", "полгода", "год"]
        )

    def test_until_is_the_last_training_day(self):
        """Окна отсчитываются назад от последнего подхода, а не от 1 января."""
        rows = stats.periods(self.conn, "Жим штанги лежа")
        last = self.conn.execute(
            "SELECT MAX(date) FROM sets WHERE exercise = 'Жим штанги лежа'"
        ).fetchone()[0]
        self.assertEqual({r.until for r in rows}, {last})
        self.assertEqual(last, stats._shift("2024-06-30", 5))

    def test_until_can_be_given(self):
        rows = stats.periods(self.conn, "Жим штанги лежа", until="2024-05-01")
        self.assertEqual({r.until for r in rows}, {"2024-05-01"})

    def test_month_counts_only_monthly_sets(self):
        """Месяц - два подхода по 100, а не все девять."""
        month = stats.periods(self.conn, "Жим штанги лежа", days=(30,))[0]
        self.assertEqual(month.sets, 2)
        self.assertEqual(month.sessions, 2)
        self.assertEqual(month.volume, 2000.0)
        self.assertEqual(month.best_rm, one_rm(100.0, 10))

    def test_longer_windows_include_shorter_ones(self):
        """Окна вложены: в квартале есть всё, что в месяце.

        Числа посчитаны по датам, которые фикстура кладёт через
        ``stats._shift``, поэтому ожидания тут не календарные, а ровно те,
        что вернула та же функция сдвига.
        """
        rows = {r.days: r for r in stats.periods(self.conn, "Жим штанги лежа")}
        self.assertEqual(rows[30].sets, 2)
        self.assertEqual(rows[90].sets, 5)
        self.assertEqual(rows[180].sets, 7)
        self.assertEqual(rows[365].sets, 9)
        self.assertLess(rows[30].sets, rows[90].sets)
        self.assertLess(rows[90].sets, rows[365].sets)

    def test_delta_is_best_rm_against_the_previous_window(self):
        rows = {r.days: r for r in stats.periods(self.conn, "Жим штанги лежа")}
        # месяц: 1RM 116.7 против 57.1 за предыдущие 30 дней
        self.assertAlmostEqual(rows[30].best_rm, one_rm(100.0, 10))
        self.assertAlmostEqual(rows[30].prev_rm, one_rm(50.0, 10))
        self.assertAlmostEqual(
            rows[30].delta, one_rm(100.0, 10) - one_rm(50.0, 10)
        )

    def test_delta_is_none_without_a_previous_window(self):
        """Первый месяц в базе - сравнивать не с чем."""
        rows = stats.periods(self.conn, "Жим штанги лежа", days=(365,))
        self.assertIsNone(rows[0].prev_rm)
        self.assertIsNone(rows[0].delta)

    def test_koi_average_and_avg_reps(self):
        month = stats.periods(self.conn, "Жим штанги лежа", days=(30,))[0]
        self.assertAlmostEqual(month.koi_average, 0.2)
        self.assertAlmostEqual(month.avg_reps, 10.0)

    def test_unknown_exercise_gives_nothing(self):
        self.assertEqual(stats.periods(self.conn, "Нет такого"), [])

    def test_outliers_and_skips_do_not_enter_the_window(self):
        self.conn.execute(
            "UPDATE sets SET is_outlier = 1, is_timed = 1"
        )
        self.conn.commit()
        month = stats.periods(self.conn, "Жим штанги лежа", days=(30,))[0]
        self.assertEqual(month.sets, 0)
        self.assertEqual(month.volume, 0.0)
        self.assertIsNone(month.best_rm)
        self.assertTrue(month.empty)

    def test_shift_moves_backwards_and_keeps_the_format(self):
        """Сдвиг - грубый, но строку он не ломает и назад не идёт."""
        for days in stats.PERIOD_DAYS:
            moved = stats._shift("2024-06-30", days)
            self.assertRegex(moved, r"^\d{4}-\d{2}-\d{2}$", days)
            self.assertLess(moved, "2024-06-30", days)

    def test_shift_zero_is_the_day_itself(self):
        self.assertEqual(stats._shift("2024-06-30", 0), "2024-06-30")


class ChartTests(unittest.TestCase):
    """ASCII-графики: библиотек нет, значит рисуем знаками."""

    def test_bar_is_empty_when_peak_is_zero(self):
        self.assertEqual(stats.ascii_bar(0.0, 0.0, width=10), " " * 10)

    def test_bar_at_the_peak_fills_the_width(self):
        self.assertEqual(stats.ascii_bar(50.0, 50.0, width=10), "#" * 10)

    def test_bar_scales_with_the_peak(self):
        self.assertEqual(stats.ascii_bar(25.0, 50.0, width=10), "#" * 5)

    def test_bar_is_clamped(self):
        """Значение выше пика не должно рисоваться шире полосы."""
        self.assertEqual(stats.ascii_bar(999.0, 50.0, width=10), "#" * 10)
        self.assertEqual(stats.ascii_bar(-5.0, 50.0, width=10), "")

    def test_format_periods_prints_table_and_two_charts(self):
        text = stats.format_periods(
            stats.periods(_build_history(), "Жим штанги лежа")
        )
        self.assertIn("период", text)
        self.assertIn("объём", text)
        self.assertIn("изменение 1RM", text)
        self.assertIn("месяц", text)
        self.assertIn("год", text)
        self.assertIn("#", text)
        self.assertTrue(text.endswith("\n"))

    def test_format_periods_says_when_there_is_nothing(self):
        self.assertIn("нет периодов", stats.format_periods([]))

    def test_growth_is_drawn_to_the_right(self):
        """Рост 1RM: плюс в числе и полоса от правого края.

        Минусовая дельта - это откат, и на графике он обязан отличаться от
        плюсовой, иначе «сброс на 6 повторов» и «вырос на 5 кг» рисуются
        одинаково.
        """
        conn = _build_history()
        try:
            rows = stats.periods(conn, "Жим штанги лежа", days=(30,))
            self.assertGreater(rows[0].delta, 0)
            line = _delta_lines(stats.format_periods(rows))[0]
            self.assertIn("+", line.split("|")[0], line)
            self.assertTrue(line.split("|")[1].strip().startswith("#"), line)
        finally:
            conn.close()

    def _history_with_one_regression(self, heavy_days: int) -> sqlite3.Connection:
        """Полугодие с откатом: раньше 100 кг, сейчас 60.

        ``heavy_days`` задаёт, когда был тяжёлый подход. Он должен лежать
        вне текущего окна, но внутри предыдущего - иначе сравнивать не с чем.
        """
        until = "2024-06-30"
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(etl.SCHEMA)
        for i, (days, weight) in enumerate(
            [(5, 60.0), (heavy_days, 100.0)], start=1
        ):
            conn.execute(
                """
                INSERT INTO sets (source, source_row, session_id, date,
                                  exercise, exercise_group, weight, reps, rm)
                 VALUES ('journal', ?, ?, ?, 'Жим штанги лежа', 'ЖИМ ЛЁЖА',
                         ?, 10, ?)
                """,
                (i, f"s{i}", stats._shift(until, days), weight,
                 one_rm(weight, 10)),
            )
        conn.commit()
        return conn

    def test_negative_delta_is_drawn_to_the_left(self):
        """Откат 1RM: минус в числе и полоса слева."""
        conn = self._history_with_one_regression(heavy_days=200)
        try:
            rows = stats.periods(conn, "Жим штанги лежа", days=(180,))
            self.assertLess(rows[0].delta, 0)
            line = _delta_lines(stats.format_periods(rows))[0]
            self.assertIn("-", line.split("|")[0], line)
            self.assertTrue(line.split("|")[1].strip().endswith("#"), line)
        finally:
            conn.close()

    def test_window_without_a_previous_one_draws_nothing(self):
        """Первое полугодие в базе: дельты нет, значит и полосы нет."""
        conn = self._history_with_one_regression(heavy_days=200)
        try:
            rows = stats.periods(conn, "Жим штанги лежа", days=(180, 360))
            self.assertIsNone(rows[1].delta)
            lines = _delta_lines(stats.format_periods(rows))
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[1].split("|")[1].strip(), "")
        finally:
            conn.close()


class RealProgramInventoryTests(unittest.TestCase):
    """Программа по настоящим данным не предлагает веса из инвентаря.

    Самое важное обещание пакета: список весов - это инвентарь зала, а не
    история записей. На синтетической базе это не проверить - там веса
    заведомо круглые. Здесь берутся все упражнения с настоящим 1RM и
    строится программа из каждого.
    """

    @classmethod
    def setUpClass(cls):
        if not Path(etl.DEFAULT_DB).exists():
            raise unittest.SkipTest(f"нет базы {etl.DEFAULT_DB}: выполните load")
        cls.conn = etl.connect(etl.DEFAULT_DB)
        cls.names = [
            row[0]
            for row in cls.conn.execute(
                """
                SELECT DISTINCT exercise
                  FROM sets
                 WHERE is_outlier = 0 AND skipped = 0 AND is_timed = 0
                   AND rm IS NOT NULL
                """
            )
        ]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_there_are_exercises_to_check(self):
        self.assertGreater(len(self.names), 50)

    def test_every_proposed_weight_is_in_stock(self):
        offenders = []
        for name in self.names:
            ladder = stats.available_weights(self.conn, name)
            plan = stats.progression_from_db(self.conn, name)
            if not plan.built:
                continue
            if not ladder:
                offenders.append((name, "программа построена без инвентаря"))
                continue
            for step in plan.program:
                for spec in step.sets:
                    if not any(abs(spec.weight - w) <= 0.3 for w in ladder):
                        offenders.append((name, spec.weight))
        self.assertEqual(offenders, [])

    def test_transition_weights_are_in_stock_too(self):
        """Волна со сбросом - это тоже программа, а не список пожеланий."""
        offenders = []
        for name in self.names[:20]:
            ladder = stats.available_weights(self.conn, name)
            plan = stats.progression_from_db(self.conn, name)
            for transition in plan.wave:
                for weight in (transition.from_weight, transition.weight):
                    if not any(abs(weight - w) <= 0.3 for w in ladder):
                        offenders.append((name, weight))
        self.assertEqual(offenders, [])


def _delta_lines(text: str) -> list[str]:
    """Строки графика дельты - те, что идут после его заголовка.

    Признак «знак перед числом» не годится: в строке
    ``месяц  +57.14  |...|`` последний символ подписи - цифра, а знак стоит
    в середине. Поэтому график вырезается по заголовку, а не по внешнему
    виду строки.
    """
    lines = text.splitlines()
    block: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            inside = "изменение 1RM" in line
            continue
        if not line.strip():
            break          # пустая строка кончает блок - дальше график объёма
        block.append(line)
    return [
        line for line in block
        if line.count("|") == 2 and line.rstrip().endswith("|")
    ]


if __name__ == "__main__":
    unittest.main()
