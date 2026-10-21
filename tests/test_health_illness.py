"""Тесты меток болезни: ``koilog.health.illness``."""

from __future__ import annotations

import unittest

from koilog.health import illness


class ZScoreTests(unittest.TestCase):
    def test_none_until_min_periods(self):
        z = illness.zscore([10.0, 12.0, 11.0], window=60, min_periods=5)
        self.assertEqual(z, [None, None, None])

    def test_spike_has_large_positive_z(self):
        baseline = [10.0, 12.0] * 10
        z = illness.zscore(baseline + [40.0])
        self.assertIsNotNone(z[-1])
        self.assertGreater(z[-1], 2.0)

    def test_dip_has_negative_z(self):
        baseline = [10.0, 12.0] * 10
        z = illness.zscore(baseline + [1.0])
        self.assertLess(z[-1], -2.0)

    def test_current_day_not_in_baseline(self):
        # один и тот же фон, но выброс не должен раздувать SD себе сам
        baseline = [10.0, 12.0] * 10
        z = illness.zscore(baseline + [40.0, 12.0])
        self.assertGreater(z[20], 5.0)


class WatchSignatureTests(unittest.TestCase):
    def test_two_moderate_breaches(self):
        moderate, strong = illness.watch_breaches(2.5, 0.0, 2.5, 0.0)
        self.assertEqual(moderate, 2)
        self.assertEqual(strong, 0)

    def test_hrv_breach_is_negative_direction(self):
        moderate, _ = illness.watch_breaches(0.0, -2.5, 0.0, 0.0)
        self.assertEqual(moderate, 1)

    def test_hrv_rise_is_not_a_breach(self):
        moderate, _ = illness.watch_breaches(0.0, 3.0, 0.0, 0.0)
        self.assertEqual(moderate, 0)

    def test_single_strong_breach(self):
        moderate, strong = illness.watch_breaches(3.5, 0.0, 0.0, 0.0)
        self.assertEqual(moderate, 1)
        self.assertEqual(strong, 1)

    def test_missing_values_are_ignored(self):
        moderate, strong = illness.watch_breaches(None, None, 2.5, None)
        self.assertEqual((moderate, strong), (1, 0))


class BreachMatrixTests(unittest.TestCase):
    """По-метричный разбор сигнатуры."""

    def test_none_when_all_normal(self):
        matrix = illness.breach_matrix(0.0, 0.0, 0.0, 0.0)
        self.assertEqual(set(matrix.values()), {"none"})

    def test_per_metric_states(self):
        # rhr умеренный, hrv сильный (падение), resp умеренный, temp норма
        matrix = illness.breach_matrix(2.5, -3.2, 2.1, 0.4)
        self.assertEqual(matrix["rhr"], "moderate")
        self.assertEqual(matrix["hrv"], "strong")
        self.assertEqual(matrix["resp"], "moderate")
        self.assertEqual(matrix["temp"], "none")

    def test_hrv_direction_is_negative(self):
        # ВСР растёт - это не нарушение
        matrix = illness.breach_matrix(0.0, 3.0, 0.0, 0.0)
        self.assertEqual(matrix["hrv"], "none")

    def test_missing_metric_is_none(self):
        matrix = illness.breach_matrix(None, None, None, None)
        self.assertEqual(set(matrix.values()), {"none"})


class CoreStrongTests(unittest.TestCase):
    """Правило «одиночный сильный сигнал» - только автономное ядро RHR/ВСР."""

    def test_strong_rhr_opens(self):
        matrix = illness.breach_matrix(3.5, 0.0, 0.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 1)

    def test_strong_hrv_opens(self):
        matrix = illness.breach_matrix(0.0, -3.5, 0.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 1)

    def test_strong_resp_alone_does_not_open(self):
        # шумная ЧДД при z>3 в одиночку эпизод не открывает
        matrix = illness.breach_matrix(0.0, 0.0, 4.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 0)

    def test_strong_temp_alone_does_not_open(self):
        matrix = illness.breach_matrix(0.0, 0.0, 0.0, 4.0)
        self.assertEqual(illness.core_strong(matrix), 0)

    def test_noisy_plus_core_counts_core(self):
        # ЧДД сильная + ВСР сильная: ядро считается (а всего 2 умеренных -> confirmed)
        matrix = illness.breach_matrix(0.0, -3.5, 4.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 1)
        moderate, _ = illness.watch_breaches(0.0, -3.5, 4.0, 0.0)
        self.assertGreaterEqual(moderate, 2)

    def test_no_strong_anywhere(self):
        matrix = illness.breach_matrix(2.5, -2.5, 2.5, 3.0)
        self.assertEqual(illness.core_strong(matrix), 0)


class GapTests(unittest.TestCase):
    def test_long_gap_found(self):
        gaps = illness.find_gaps(["2024-01-01", "2024-01-20"])
        self.assertEqual(gaps, [("2024-01-02", "2024-01-19")])

    def test_short_gap_ignored(self):
        self.assertEqual(illness.find_gaps(["2024-01-01", "2024-01-10"]), [])

    def test_exactly_min_days(self):
        # 2024-01-02 … 2024-01-15 - ровно 14 дней
        gaps = illness.find_gaps(["2024-01-01", "2024-01-16"])
        self.assertEqual(gaps, [("2024-01-02", "2024-01-15")])


class MergeTests(unittest.TestCase):
    def test_adjacent_days_merge(self):
        episodes = illness.merge_episodes(
            {"2024-02-01": "confirmed", "2024-02-02": "confirmed", "2024-02-10": "probable"},
            [],
        )
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].start, "2024-02-01")
        self.assertEqual(episodes[0].end, "2024-02-02")
        self.assertEqual(episodes[0].days, 2)

    def test_level_is_the_maximum(self):
        episodes = illness.merge_episodes(
            {"2024-02-01": "probable", "2024-02-02": "confirmed"}, []
        )
        self.assertEqual(episodes[0].level, "confirmed")

    def test_gap_becomes_gap_only(self):
        episodes = illness.merge_episodes({}, [("2024-05-01", "2024-05-20")])
        self.assertEqual(episodes[0].level, "gap_only")
        self.assertEqual(episodes[0].source, "gap")

    def test_gap_covered_by_confirmed_day_is_skipped(self):
        episodes = illness.merge_episodes(
            {"2024-02-05": "confirmed"}, [("2024-02-01", "2024-02-10")]
        )
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].level, "confirmed")

    def test_gap_with_watch_deviation_is_probable(self):
        episodes = illness.merge_episodes(
            {}, [("2024-05-01", "2024-05-20")], {"2024-05-01": "probable"}
        )
        self.assertEqual(episodes[0].level, "probable")
        self.assertEqual(episodes[0].source, "gap+watch")


class HoldingDayTests(unittest.TestCase):
    """Одностороннее правило «держит» для хвоста (4.1) = правило гейта."""

    def test_rhr_above_background_holds(self):
        self.assertTrue(illness._holding_day(1.5, None, None))

    def test_hrv_below_background_holds(self):
        self.assertTrue(illness._holding_day(None, -1.5, None))

    def test_wrist_temp_high_holds(self):
        self.assertTrue(illness._holding_day(None, None, 3.0))

    def test_low_rhr_does_not_hold(self):
        # низкий пульс - «отдохнул», не держит (симметричный abs отклонён D10)
        self.assertFalse(illness._holding_day(-2.0, None, None))

    def test_high_hrv_does_not_hold(self):
        # высокая ВСР после болезни - отскок, не держит
        self.assertFalse(illness._holding_day(None, 3.0, None))

    def test_all_none_does_not_hold(self):
        self.assertFalse(illness._holding_day(None, None, None))


class ExtendTailsTests(unittest.TestCase):
    """Продление эпизода «держащим» хвостом (4.1, чистая функция).

    Экспонаты — эп-34 (класс отказа «разметка короче деривации»), эп-26
    (предохранитель слияния) и cap ``TAIL_MAX``.
    """

    @staticmethod
    def _ep(start, end, level="confirmed"):
        return illness.Episode(start, end, level, "watch/temp")

    def test_sick_tail_extends_episode_without_level_upgrade(self):
        # эп-34-класс: за маркированным end идёт серия держащих ночей.
        ep = self._ep("2026-10-11", "2026-10-13")
        holding = {"2026-10-14", "2026-10-15"}
        result = illness.extend_tails([ep], holding)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["days"], ["2026-10-14", "2026-10-15"])
        # уровень эпизода не меняется (ретро-апгрейда нет)
        self.assertEqual(ep.level, "confirmed")
        self.assertEqual(illness.TAIL_LEVEL, "watch_tail")

    def test_single_holding_night_is_not_a_tail(self):
        # серия короче SPAN_MIN_DAYS (=2) - фоновый шум, не сигнал
        ep = self._ep("2026-10-11", "2026-10-13")
        result = illness.extend_tails([ep], {"2026-10-14"})
        self.assertEqual(result, [])

    def test_fuse_truncates_to_gap_minus_one(self):
        # эп-14-класс: разрыв 4, хвост 3 -> limit = разрыв-1 = 3, помещается ровно
        first = self._ep("2024-11-27", "2024-11-28", "probable")
        second = self._ep("2024-12-02", "2024-12-03", "probable")
        holding = {"2024-11-29", "2024-11-30", "2024-12-01"}
        result = illness.extend_tails([first, second], holding)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["days"],
                         ["2024-11-29", "2024-11-30", "2024-12-01"])

    def test_fuse_drops_tail_entirely_when_remainder_too_short(self):
        # эп-26-класс: разрыв 2 -> limit = 1 < SPAN_MIN_DAYS -> хвост целиком нет
        first = self._ep("2026-02-11", "2026-02-13", "probable")
        second = self._ep("2026-02-15", "2026-02-21")
        holding = {"2026-02-14", "2026-02-15"}
        result = illness.extend_tails([first, second], holding)
        self.assertEqual(result, [])

    def test_tail_max_caps_the_extension(self):
        # эп-12-класс длиннее cap: хвост > TAIL_MAX не расширяется
        ep = self._ep("2024-07-17", "2024-07-22")
        holding = {f"2024-07-{d:02d}" for d in range(23, 31)}  # 8 ночей
        result = illness.extend_tails([ep], holding)
        self.assertEqual(len(result[0]["days"]), illness.TAIL_MAX)

    def test_continuous_run_stops_at_first_gap(self):
        ep = self._ep("2024-07-17", "2024-07-22")
        # 23-24 держат, 25 нет -> серия 2 (>= SPAN_MIN_DAYS), 26+ не берём
        holding = {"2024-07-23", "2024-07-24", "2024-07-26"}
        result = illness.extend_tails([ep], holding)
        self.assertEqual(result[0]["days"], ["2024-07-23", "2024-07-24"])

    def test_last_episode_uses_full_tail_max(self):
        ep = self._ep("2024-07-17", "2024-07-22")
        holding = {f"2024-07-{d:02d}" for d in range(23, 31)}
        result = illness.extend_tails([ep], holding)
        # нет следующего эпизода -> limit = TAIL_MAX
        self.assertEqual(len(result[0]["days"]), illness.TAIL_MAX)


class TailDriftGuardTests(unittest.TestCase):
    """Дрифт-гард: label-хвост (illness) и state-хвост (state) не расползаются.

    Импорт ``state`` в тесте (не в прод-коде illness) - намеренно: это защита
    от расхождения двух констант, а не зависимость.
    """

    def test_tail_max_matches_sick_tail_max(self):
        from koilog.health import state
        self.assertEqual(illness.TAIL_MAX, state.SICK_TAIL_MAX)

    def test_span_min_days_matches_gate_nights(self):
        from koilog.health import retrain
        self.assertEqual(illness.SPAN_MIN_DAYS, retrain.GATE_NIGHTS)

    def test_gate_z_tail_matches_gate_z(self):
        from koilog.health import retrain
        self.assertEqual(illness.GATE_Z_TAIL, retrain.GATE_Z)


class BuildTailTests(unittest.TestCase):
    """Интеграция 4.1: ``illness.build`` реально продлевает end и пишет хвост."""

    def _db(self) -> "sqlite3.Connection":
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE aw_daily (
                date       TEXT PRIMARY KEY,
                rhr        REAL,
                hrv_sdnn   REAL,
                resp_rate  REAL,
                wrist_temp REAL,
                sleep_hours REAL
            );
            CREATE TABLE aw_bodytemp (date TEXT, value REAL);
            CREATE TABLE sets (exercise TEXT, date TEXT, weight REAL, reps INTEGER,
                rm REAL, rm_session REAL, set_index INTEGER, koi REAL,
                is_warmup INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0);
            """
        )
        return conn

    def test_build_extends_end_and_marks_tail_days(self):
        from datetime import date as date_cls
        from datetime import timedelta
        import sqlite3  # noqa: F401

        conn = self._db()
        # фон 60 дней «нейтральных» ночей, затем эпизод и держащий хвост
        start = date_cls.fromisoformat("2026-01-01")
        day = start
        for i in range(60):
            conn.execute(
                "INSERT INTO aw_daily (date, rhr, hrv_sdnn, resp_rate, wrist_temp, sleep_hours) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), 60.0, 70.0, 15.0, 35.5, 7.5),
            )
            day += timedelta(days=1)
        # эпизод: сильный RHR-выброс 2 дня (confirmed по сигнатуре: rhr z>2)
        for _ in range(2):
            conn.execute(
                "INSERT INTO aw_daily (date, rhr, hrv_sdnn, resp_rate, wrist_temp, sleep_hours) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), 95.0, 70.0, 15.0, 35.5, 7.5),
            )
            day += timedelta(days=1)
        # хвост: 2 ночи RHR z>1 (держит, но уровень не подтверждают)
        tail_days = []
        for _ in range(2):
            conn.execute(
                "INSERT INTO aw_daily (date, rhr, hrv_sdnn, resp_rate, wrist_temp, sleep_hours) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), 72.0, 70.0, 15.0, 35.5, 7.5),
            )
            tail_days.append(day.isoformat())
            day += timedelta(days=1)
        # тренировочные дни (без больших перерывов)
        conn.execute("INSERT INTO sets (exercise, date) VALUES ('Жим', ?)",
                     ((start).isoformat(),))

        episodes = illness.build(conn, window=None)
        self.assertEqual(len(episodes), 1)
        ep = episodes[0]
        self.assertEqual(ep.end, tail_days[-1])
        # уровень эпизода не повышен из-за хвоста (хвост сам его не задаёт)
        self.assertIn(ep.level, ("confirmed", "probable"))

        rows = {r["date"]: r["level"] for r in conn.execute(
            "SELECT date, level FROM illness_day")}
        for d in tail_days:
            self.assertEqual(rows[d], illness.TAIL_LEVEL)


if __name__ == "__main__":
    unittest.main()
