"""Тесты возврата к весам: ``koilog.health.retrain``.

Чистые функции (полосы рампы, бины детрейна, разметка возвратов) проверяются
на числах; работа с базой - на синтетической: ``sets`` с разрывами и ``aw_daily``
с ровным фоном. Здесь намеренно нет «красивых» больших чисел: у автора эпизодов
единицы, и тест фиксирует логику, а не статистику.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog.health import retrain


def _calendar(start: str, count: int) -> list[str]:
    first = date_cls.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            exercise   TEXT,
            date       TEXT,
            weight     REAL,
            reps       INTEGER,
            rm         REAL,
            rm_session REAL,
            set_index  INTEGER,
            koi        REAL,
            is_warmup  INTEGER NOT NULL DEFAULT 0,
            skipped    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE aw_daily (
            date       TEXT PRIMARY KEY,
            rhr        REAL,
            hrv_sdnn   REAL,
            wrist_temp REAL
        );
        CREATE TABLE illness_episodes (
            id     INTEGER PRIMARY KEY,
            start  TEXT NOT NULL,
            end    TEXT NOT NULL,
            level  TEXT NOT NULL,
            source TEXT,
            type   TEXT NOT NULL DEFAULT 'unknown',
            notes  TEXT
        );
        """
    )
    return conn


def _add_set(conn, exercise, day, weight, reps, rm, *, warmup=False, set_index=1,
             koi=None):
    conn.execute(
        "INSERT INTO sets (exercise, date, weight, reps, rm, rm_session, "
        "is_warmup, set_index, koi) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (exercise, day, weight, reps, rm, rm, int(warmup), set_index, koi),
    )


def _add_episode(conn, start, end, level="confirmed"):
    conn.execute(
        "INSERT INTO illness_episodes (start, end, level, source, type) "
        "VALUES (?, ?, ?, 'test', 'unknown')",
        (start, end, level),
    )


def _flat_physiology(conn, start: str, count: int) -> list[str]:
    """Ровный фон с лёгкой вариацией, чтобы SD была не нулевой.

    Полностью константный ряд дал бы ``z = None`` (делить на нулевой SD
    нельзя), и гейт отвечал бы «нет фона» - это не то, что проверяем.
    Последние две ночи ставим ровно на фон: тесты гейта проверяют решение
    «можно / рано», а не арифметику z на границе ±1.
    """
    days = _calendar(start, count)
    for index, day in enumerate(days):
        conn.execute(
            "INSERT INTO aw_daily (date, rhr, hrv_sdnn, wrist_temp) "
            "VALUES (?, ?, ?, ?)",
            (day, 60.0 + (index % 2) * 2.0, 70.0 - (index % 2) * 2.0,
             35.5 + (index % 2) * 0.1),
        )
    for day in days[-2:]:
        conn.execute(
            "UPDATE aw_daily SET rhr = 61.0, hrv_sdnn = 69.0, "
            "wrist_temp = 35.55 WHERE date = ?",
            (day,),
        )
    return days


class RampTests(unittest.TestCase):
    def test_bands_by_days_off(self):
        self.assertEqual(retrain.ramp_for(3).label, "до недели")
        self.assertEqual(retrain.ramp_for(7).label, "1–2 недели")
        self.assertEqual(retrain.ramp_for(13).label, "1–2 недели")
        self.assertEqual(retrain.ramp_for(14).label, "2–4 недели")
        self.assertEqual(retrain.ramp_for(27).label, "2–4 недели")
        self.assertEqual(retrain.ramp_for(28).label, "больше месяца")

    def test_longer_gap_starts_lighter(self):
        short = retrain.ramp_for(3)
        long = retrain.ramp_for(40)
        self.assertLess(long.start_pct, short.start_pct)
        self.assertGreaterEqual(long.weeks, short.weeks)


class RampProgramTests(unittest.TestCase):
    """Рампа возврата обязана дойти до предболезненного веса внутри полосы."""

    LADDER = [20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0]

    def test_reaches_the_base_within_the_band(self):
        # 30 -> 50: при двух неделях полосы - две ступени, старт и база
        program = retrain.ramp_program(
            self.LADDER, 30.0, 50.0, 8, sets=3, weeks=2, reference_rm=60.0)
        self.assertEqual(len(program), 2)
        self.assertAlmostEqual(program[-1].weight, 50.0)
        # тяжёлая неделя (40/45/50) при предболезненных 8 повторах даёт
        # КОИ 1.04 («Load») - чтобы остаться в Optimal, повторы падают до 7
        self.assertEqual(program[-1].reps, 7)

    def test_one_step_per_week_of_the_band(self):
        # шагов ровно столько, сколько недель в полосе (упражнение раз в неделю)
        for weeks in (2, 3, 4):
            program = retrain.ramp_program(
                self.LADDER, 20.0, 50.0, 8, sets=3, weeks=weeks,
                reference_rm=60.0)
            self.assertEqual(len(program), weeks)
            self.assertAlmostEqual(program[-1].weight, 50.0)

    def test_skips_rungs_but_still_lands_on_the_base(self):
        # весов (ступеней) больше, чем шагов: промежуточные пропускаются,
        # но последний шаг - всё равно полный вес
        program = retrain.ramp_program(
            self.LADDER, 20.0, 50.0, 8, sets=3, weeks=1, reference_rm=60.0)
        self.assertEqual(len(program), 2)
        weights = [step.weight for step in program]
        self.assertEqual(weights, sorted(weights))
        self.assertEqual(len(set(weights)), len(weights))
        self.assertAlmostEqual(weights[-1], 50.0)

    def test_long_band_does_not_repeat_a_step(self):
        # полоса длиннее лестницы: вес добирается за span+1 шагов,
        # одинаковых шагов (одинаковых лесенок) нет
        program = retrain.ramp_program(
            [30.0, 35.0, 40.0], 30.0, 40.0, 8, sets=3, weeks=4,
            reference_rm=60.0)
        steps = [step.sets for step in program]
        self.assertEqual(len(steps), 3)
        self.assertEqual(len(set(steps)), len(steps))
        self.assertAlmostEqual(program[-1].weight, 40.0)

    def test_sets_form_a_ladder_up_to_the_week_target(self):
        # подходы внутри шага - лесенка, дорастающая до цели недели
        # (верх = цель, ниже - ступени вниз), у нижнего края - дубли
        program = retrain.ramp_program(
            self.LADDER, 30.0, 50.0, 8, sets=3, weeks=4, reference_rm=60.0)
        self.assertEqual([s.weight for s in program[0].sets],
                         [20.0, 25.0, 30.0])
        self.assertEqual([s.weight for s in program[1].sets],
                         [25.0, 30.0, 35.0])
        self.assertEqual([s.weight for s in program[-1].sets],
                         [40.0, 45.0, 50.0])
        # верхний подход недели - рабочий вес недели
        self.assertEqual(program[1].sets[-1].weight, 35.0)
        # верх предыдущей недели остаётся внутри лесенки следующей
        self.assertIn(program[0].sets[-1].weight,
                      [s.weight for s in program[1].sets])

    def test_reps_are_capped_and_keep_the_session_optimal(self):
        # повторы подбираются так, чтобы КОИ тренировки остался в Optimal
        # и не выше предболезненного числа повторов
        program = retrain.ramp_program(
            self.LADDER, 30.0, 50.0, 9, sets=3, weeks=2, reference_rm=60.0)
        for step in program:
            self.assertLessEqual(step.reps, 9)
            self.assertLess(step.koi, 1.0)
            self.assertEqual(step.band, "Optimal")
        # тяжёлая неделя идёт с меньшим числом повторов, чем лёгкая
        self.assertLess(program[-1].reps, program[0].reps)

    def test_off_ladder_base_snaps_to_the_lighter_rung(self):
        # 47 кг нет на лестнице: целься в 45, а не в 50
        program = retrain.ramp_program(
            self.LADDER, 30.0, 47.0, 8, sets=3, weeks=3, reference_rm=60.0)
        self.assertAlmostEqual(program[-1].weight, 45.0)

    def test_koi_grows_with_the_load(self):
        program = retrain.ramp_program(
            self.LADDER, 30.0, 50.0, 8, sets=1, weeks=2, reference_rm=60.0)
        kois = [step.koi for step in program]
        self.assertEqual(kois, sorted(kois))


class DetrainPointTests(unittest.TestCase):
    def test_drop_kg_and_pct(self):
        point = retrain.DetrainPoint("Жим", 10, 100.0, 90.0)
        self.assertAlmostEqual(point.drop_kg, 10.0)
        self.assertAlmostEqual(point.drop_pct, 0.1)

    def test_zero_base_does_not_divide(self):
        point = retrain.DetrainPoint("Жим", 10, 0.0, 0.0)
        self.assertEqual(point.drop_pct, 0.0)


class DetrainBinsTests(unittest.TestCase):
    def test_points_fall_into_their_bins(self):
        points = [
            retrain.DetrainPoint("A", 3, 100.0, 95.0),    # <7
            retrain.DetrainPoint("A", 10, 100.0, 90.0),   # 7-14
            retrain.DetrainPoint("A", 20, 100.0, 80.0),   # 14-28
            retrain.DetrainPoint("A", 40, 100.0, 70.0),   # >28
        ]
        bins = {b["label"]: b for b in retrain.detraining_bins(points)}
        self.assertEqual(bins["<7 дн"]["n"], 1)
        self.assertAlmostEqual(bins["<7 дн"]["mean"], 0.05)
        self.assertAlmostEqual(bins["7–14 дн"]["median"], 0.10)
        self.assertAlmostEqual(bins["14–28 дн"]["mean"], 0.20)
        self.assertAlmostEqual(bins[">28 дн"]["mean"], 0.30)

    def test_empty_bin_reports_none(self):
        bins = retrain.detraining_bins([])
        self.assertEqual(len(bins), 4)
        for bucket in bins:
            self.assertEqual(bucket["n"], 0)
            self.assertIsNone(bucket["mean"])
            self.assertIsNone(bucket["median"])

    def test_median_of_even_count(self):
        points = [
            retrain.DetrainPoint("A", 1, 100.0, 90.0),
            retrain.DetrainPoint("A", 2, 100.0, 80.0),
        ]
        bins = {b["label"]: b for b in retrain.detraining_bins(points)}
        self.assertAlmostEqual(bins["<7 дн"]["median"], 0.15)


class DetrainingCurveTests(unittest.TestCase):
    def test_gap_between_sessions_becomes_a_point(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)
        _add_set(conn, "Жим гантелей лежа", "2024-01-20", 36.0, 8, 45.0)

        points = retrain.detraining_curve(conn)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].days_off, 19)
        self.assertAlmostEqual(points[0].pre_rm, 50.0)
        self.assertAlmostEqual(points[0].post_rm, 45.0)
        self.assertAlmostEqual(points[0].drop_pct, 0.1)

    def test_short_gap_is_not_a_detrain_point(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)
        _add_set(conn, "Жим гантелей лежа", "2024-01-03", 40.0, 8, 51.0)
        self.assertEqual(retrain.detraining_curve(conn), [])


class BaselineGateTests(unittest.TestCase):
    def test_ready_when_last_nights_in_baseline(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)

        gate = retrain.baseline_gate(conn, today=days[-1])
        self.assertTrue(gate.ready, gate.reasons)
        self.assertEqual(gate.nights, 2)
        self.assertEqual(gate.reasons, ())

    def test_hold_when_rhr_spikes_last_night(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        conn.execute("UPDATE aw_daily SET rhr = 100.0 WHERE date = ?", (days[-1],))

        gate = retrain.baseline_gate(conn, today=days[-1])
        self.assertFalse(gate.ready)
        self.assertTrue(any("RHR" in reason for reason in gate.reasons))

    def test_hold_when_no_watch_data(self):
        conn = _db()
        self.addCleanup(conn.close)
        gate = retrain.baseline_gate(conn, today="2024-01-30")
        self.assertFalse(gate.ready)
        self.assertTrue(any("нет данных" in reason for reason in gate.reasons))

    def test_active_episode_blocks_return(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        _add_episode(conn, "2024-01-25", "2024-02-05")

        gate = retrain.baseline_gate(conn, today=days[-1])
        self.assertFalse(gate.ready)
        self.assertTrue(any("эпизод" in reason for reason in gate.reasons))

    # -- односторонний гейт: «плохо» только RHR вверх и ВСР вниз ------------

    def test_high_hrv_does_not_block(self):
        """Высокая ВСР - не повод держать (это «отдохнул», а не «заболевает»)."""
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        for day in days[-2:]:
            conn.execute("UPDATE aw_daily SET hrv_sdnn = 74.0 WHERE date = ?",
                         (day,))

        gate = retrain.baseline_gate(conn, today=days[-1], silence_days=0)
        self.assertTrue(gate.ready, gate.reasons)

    def test_low_hrv_blocks(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        for day in days[-2:]:
            conn.execute("UPDATE aw_daily SET hrv_sdnn = 64.0 WHERE date = ?",
                         (day,))

        gate = retrain.baseline_gate(conn, today=days[-1], silence_days=0)
        self.assertFalse(gate.ready)
        self.assertTrue(any("ВСР" in reason for reason in gate.reasons))

    def test_low_rhr_does_not_block(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        for day in days[-2:]:
            conn.execute("UPDATE aw_daily SET rhr = 57.0 WHERE date = ?", (day,))

        gate = retrain.baseline_gate(conn, today=days[-1], silence_days=0)
        self.assertTrue(gate.ready, gate.reasons)

    def test_high_wrist_temp_blocks(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        conn.execute("UPDATE aw_daily SET wrist_temp = 37.5 WHERE date = ?",
                     (days[-1],))

        gate = retrain.baseline_gate(conn, today=days[-1], silence_days=0)
        self.assertFalse(gate.ready)
        self.assertTrue(any("t запястья" in reason for reason in gate.reasons))

    # -- окно тишины после эпизода (вариант B) ------------------------------

    def test_silence_window_blocks_return_by_ramp(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        _add_episode(conn, "2024-01-20", days[-3])   # конец за 2 дня до сегодня

        gate = retrain.baseline_gate(conn, today=days[-1])  # дефолтное окно
        self.assertFalse(gate.ready)
        self.assertIsNotNone(gate.silence_until)
        self.assertTrue(gate.window_only, gate.reasons)
        self.assertTrue(all(r.startswith("окно тишины") for r in gate.reasons))

    def test_silence_window_is_over_after_n_days(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        _add_episode(conn, "2024-01-10", "2024-01-11")   # окно кончилось 25.01

        gate = retrain.baseline_gate(conn, today=days[-1])  # 30.01
        self.assertTrue(gate.ready, gate.reasons)
        self.assertIsNone(gate.silence_until)

    def test_window_is_not_only_when_channels_also_block(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        _add_episode(conn, "2024-01-20", days[-3])
        conn.execute("UPDATE aw_daily SET rhr = 100.0 WHERE date = ?",
                     (days[-1],))   # плюс к окну ещё и RHR вверх

        gate = retrain.baseline_gate(conn, today=days[-1])
        self.assertFalse(gate.ready)
        self.assertFalse(gate.window_only)
        self.assertTrue(any("RHR" in r for r in gate.reasons))

    def test_zero_silence_days_switches_window_off(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        _add_episode(conn, "2024-01-20", days[-3])

        gate = retrain.baseline_gate(conn, today=days[-1], silence_days=0)
        self.assertTrue(gate.ready, gate.reasons)

    def test_silence_window_takes_the_latest_episode_end(self):
        """Если эпизоды идут близко, действует самое позднее окончание окна."""
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        _add_episode(conn, "2024-01-22", "2024-01-24")   # окно до 2024-02-07
        _add_episode(conn, "2024-01-25", "2024-01-26")   # окно до 2024-02-09

        gate = retrain.baseline_gate(conn, today=days[-1])   # 30.01
        self.assertFalse(gate.ready)
        self.assertEqual(gate.silence_until, "2024-02-09")

    def test_gate_serializes_to_dict(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        payload = retrain.baseline_gate(conn, today=days[-1]).to_dict()
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["today"], days[-1])
        self.assertEqual(len(payload["rows"]), 2)


class ReturnPlanTests(unittest.TestCase):
    def test_ramp_starts_below_pre_illness_weight(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)

        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")

        # простой 24 дня -> полоса «2–4 недели», 85 % от 40 кг = 34 кг,
        # в инвентаре (гантельная лесенка) - ступень 30 кг
        self.assertEqual(plan.days_off, 24)
        self.assertEqual(plan.ramp.label, "2–4 недели")
        self.assertAlmostEqual(plan.base_weight, 40.0)
        self.assertAlmostEqual(plan.start_weight, 30.0)
        self.assertIsNotNone(plan.program)
        self.assertGreater(len(plan.program), 0)
        self.assertIsNotNone(plan.gate)

    def test_pre_illness_weight_is_the_best_set_not_the_first(self):
        """Регресс: база - лучший подход дня, а не первый (разминочный).

        ``rm_session`` одинакова на всех подходах сессии, поэтому старая
        сортировка по ней отдавала произвольный (первый) подход.
        """
        conn = _db()
        self.addCleanup(conn.close)
        # Первый подход лёгкий, последний - рабочий; все не разминочные.
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 20.0, 10, 26.0,
                 set_index=1)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 30.0, 10, 40.0,
                 set_index=2)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 6, 48.0,
                 set_index=3)

        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")

        self.assertAlmostEqual(plan.base_weight, 40.0)
        self.assertEqual(plan.base_reps, 6)
        self.assertAlmostEqual(plan.base_rm, 48.0)

    def test_warmup_is_ignored_even_if_it_has_high_rm(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 45.0, 8, 57.0,
                 warmup=True, set_index=1)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 30.0, 10, 40.0,
                 set_index=2)

        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")

        self.assertAlmostEqual(plan.base_weight, 30.0)

    def test_no_external_weight_is_reported(self):
        conn = _db()
        self.addCleanup(conn.close)
        # у планки нет внешнего веса - рампа не строится, но это не падение
        _add_set(conn, "Планка", "2024-01-01", 0.0, 0, 1.0)

        plan = retrain.return_plan(conn, "Планка", today="2024-01-25")
        self.assertIsNone(plan.program)
        self.assertIn("внешнего веса", plan.reason)

    def test_missing_exercise_is_reported(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)

        plan = retrain.return_plan(conn, "Несуществующее", today="2024-01-25")
        self.assertIsNone(plan.program)
        self.assertIn("не от чего", plan.reason)

    def test_default_exercise_is_most_trained(self):
        conn = _db()
        self.addCleanup(conn.close)
        for day in _calendar("2024-01-01", 3):
            _add_set(conn, "Приседания", day, 60.0, 5, 70.0)
        _add_set(conn, "Жим гантелей лежа", "2024-01-05", 40.0, 8, 50.0)

        self.assertEqual(retrain.default_exercise(conn), "Приседания")

    def test_plan_serializes_to_dict(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)
        payload = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25").to_dict()
        self.assertEqual(payload["exercise"], "Жим гантелей лежа")
        self.assertIn("gate", payload)
        self.assertIn("base_koi", payload)
        self.assertGreater(len(payload["steps"]), 0)

    def test_full_return_happens_within_the_band(self):
        """Регресс: рампа доходит до полного веса, а не обрывается на лёгком."""
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 44.0, 8, 55.0)

        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")

        self.assertIsNotNone(plan.full_return_step)
        self.assertLessEqual(plan.full_return_step, plan.ramp.weeks)
        self.assertGreaterEqual(plan.program[-1].weight,
                                plan.base_weight - 1e-9)

    def test_base_koi_and_week_are_reported(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0,
                 koi=0.9)
        _add_set(conn, "Жим гантелей лежа", "2024-01-02", 35.0, 8, 45.0,
                 koi=0.4)

        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")

        # база - последняя тренировка до болезни (2024-01-02)
        self.assertAlmostEqual(plan.base_koi, 0.4)
        # обе даты в одной ISO-неделе - КОИ за неделю складывается
        self.assertAlmostEqual(plan.base_week_koi, 1.3)
        self.assertEqual(plan.base_week, "2024-W01")

    def test_missing_koi_column_does_not_crash(self):
        conn = _db()
        conn.execute("ALTER TABLE sets DROP COLUMN koi")
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO sets (exercise, date, weight, reps, rm, rm_session, "
            "is_warmup, set_index) "
            "VALUES ('Жим гантелей лежа', '2024-01-01', 40.0, 8, 50.0, 50.0, "
            "0, 1)")

        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")

        self.assertIsNone(plan.base_koi)
        self.assertIsNone(plan.base_week_koi)


class ReturnKindTests(unittest.TestCase):
    def _row(self, first_rm: float, pre_rm: float = 100.0) -> retrain.ReturnRow:
        return retrain.ReturnRow(
            episode_start="2024-01-05", episode_end="2024-01-10",
            level="confirmed", base_date="2024-01-01", first_date="2024-01-15",
            days_off=14, pre_rm=pre_rm, first_rm=first_rm, horizon_rm=first_rm,
            speed_pct_week=0.0, rebound_in=None,
        )

    def test_hard_when_returning_near_pre_illness(self):
        self.assertEqual(self._row(80.0).kind, "жёсткий")

    def test_soft_when_starting_well_below(self):
        self.assertEqual(self._row(55.0).kind, "мягкий")

    def test_middle_between_thresholds(self):
        self.assertEqual(self._row(70.0).kind, "средний")

    def test_first_ratio_and_dict(self):
        row = self._row(75.0)
        self.assertAlmostEqual(row.first_ratio, 0.75)
        payload = row.to_dict()
        self.assertEqual(payload["kind"], "жёсткий")
        self.assertAlmostEqual(payload["first_ratio"], 0.75)


class ReturnsTests(unittest.TestCase):
    def test_episode_between_sessions_is_marked(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-05", 40.0, 8, 50.0)
        _add_set(conn, "Жим гантелей лежа", "2024-01-20", 30.0, 8, 30.0)
        _add_episode(conn, "2024-01-10", "2024-01-14")

        rows = retrain.returns(conn, "Жим гантелей лежа")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].first_date, "2024-01-20")
        self.assertEqual(rows[0].days_off, 15)
        self.assertAlmostEqual(rows[0].first_ratio, 0.6)
        self.assertEqual(rows[0].kind, "мягкий")
        self.assertIsNone(rows[0].rebound_in)

    def test_rebound_is_days_to_next_episode(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-05", 40.0, 8, 50.0)
        _add_set(conn, "Жим гантелей лежа", "2024-01-20", 40.0, 8, 48.0)
        _add_episode(conn, "2024-01-10", "2024-01-14")
        _add_episode(conn, "2024-01-25", "2024-01-27")

        rows = retrain.returns(conn, "Жим гантелей лежа")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].rebound_in, 5)

    def test_gap_only_is_ignored(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-05", 40.0, 8, 50.0)
        _add_set(conn, "Жим гантелей лежа", "2024-01-20", 40.0, 8, 48.0)
        _add_episode(conn, "2024-01-10", "2024-01-14", level="gap_only")
        self.assertEqual(retrain.returns(conn, "Жим гантелей лежа"), [])


class MinimalSchemaTests(unittest.TestCase):
    """База с одними ``sets``: команда не должна падать на health-таблицах."""

    def _sets_only(self) -> sqlite3.Connection:
        conn = _db()
        conn.execute("DROP TABLE aw_daily")
        conn.execute("DROP TABLE illness_episodes")
        return conn

    def test_gate_reports_no_watch_data(self):
        conn = self._sets_only()
        self.addCleanup(conn.close)
        gate = retrain.baseline_gate(conn, today="2024-01-30")
        self.assertFalse(gate.ready)
        self.assertTrue(any("нет данных" in reason for reason in gate.reasons))

    def test_return_plan_still_builds(self):
        conn = self._sets_only()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)
        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")
        self.assertIsNotNone(plan.program)

    def test_returns_is_empty_without_episodes_table(self):
        conn = self._sets_only()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)
        self.assertEqual(retrain.returns(conn, "Жим гантелей лежа"), [])


class FormatTests(unittest.TestCase):
    def _plan(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0)
        return retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")

    def test_format_return_has_headline_and_rules(self):
        text = retrain.format_return(self._plan())
        self.assertIn("возврат к весам", text)
        self.assertIn("предболезненный вес", text)
        self.assertIn("гейт", text)
        self.assertIn("рампа", text)

    def test_format_return_shows_koi_and_full_return_time(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add_set(conn, "Жим гантелей лежа", "2024-01-01", 40.0, 8, 50.0,
                 koi=0.9)
        plan = retrain.return_plan(
            conn, "Жим гантелей лежа", today="2024-01-25")
        text = retrain.format_return(plan)
        self.assertIn("КОИ упражнения", text)
        self.assertIn("КОИ за неделю", text)
        self.assertIn("полный вес достигается", text)
        self.assertNotIn("не достигается", text)

    def test_format_curve_without_points(self):
        text = retrain.format_curve([], "Жим гантелей лежа")
        self.assertIn("нет простоев", text)

    def test_format_curve_with_points_lists_anchors(self):
        points = [retrain.DetrainPoint("Жим", 20, 100.0, 85.0)]
        text = retrain.format_curve(points, "Жим")
        self.assertIn("литературные ориентиры", text)
        self.assertIn("Mujika", text)

    def test_format_gate_shows_verdict(self):
        conn = _db()
        self.addCleanup(conn.close)
        days = _flat_physiology(conn, "2024-01-01", 30)
        text = retrain.format_gate(retrain.baseline_gate(conn, today=days[-1]))
        self.assertIn("можно возвращаться", text)

    def test_format_returns_empty(self):
        self.assertIn("нет эпизодов", retrain.format_returns([]))

    def test_dumps_handles_plan_and_list(self):
        import json

        payload = json.loads(retrain.dumps(self._plan()))
        self.assertEqual(payload["exercise"], "Жим гантелей лежа")
        rows = [retrain.DetrainPoint("Жим", 10, 100.0, 90.0)]
        # DetrainPoint не имеет to_dict - просто проверяем, что не падает
        self.assertIsInstance(retrain.dumps(rows), str)


if __name__ == "__main__":
    unittest.main()
