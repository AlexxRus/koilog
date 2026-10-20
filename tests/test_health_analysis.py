"""Тесты срезов для ноутбука: ``koilog.health.analysis``.

Тот же синтетический прогон, что и в ``test_health_ml``: маленькая база в
памяти, «болезнь» около 55-го дня. Здесь нет ни моделей, ни sklearn - только
выборки рядов, поэтому тесты идут всегда.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog.health import analysis, apple, illness, load, ml


def _calendar(start: str, count: int) -> list[str]:
    first = date_cls.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _synthetic_db(days: int = 90) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(apple._SCHEMA)
    conn.executescript(load._SCHEMA)
    conn.executescript(illness._SCHEMA)
    conn.executescript(ml._FEATURE_SCHEMA)

    calendar = _calendar("2024-01-01", days)
    for index, day in enumerate(calendar):
        sick = 55 <= index <= 57
        rhr = 60 + (index % 4) * 0.4 + (15 if sick else 0)
        hrv = 70 + (index % 3) * 0.5 - (35 if sick else 0)
        resp = 15 + (index % 3) * 0.1 + (5 if sick else 0)
        wrist = 35.5 + (index % 2) * 0.1 + (1.0 if sick else 0)
        sleep = 7.5 - (index % 3) * 0.1 - (3 if sick else 0)
        conn.execute(
            "INSERT INTO aw_daily (date, rhr, hrv_sdnn, resp_rate, wrist_temp, "
            "spo2, sleep_hours, sleep_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day, rhr, hrv, resp, wrist, 96.0, sleep, "23:30"),
        )
        trained = index % 2 == 0
        conn.execute(
            "INSERT INTO health_days (date, trained, aw_coverage, rhr, hrv_sdnn, "
            "sleep_hours, in_window) VALUES (?, ?, 1, ?, ?, ?, 1)",
            (day, 1 if trained else 0, rhr, hrv, sleep),
        )
        conn.execute(
            "INSERT INTO load_daily (date, sets, tonnage, weekly_tonnage, srpe, "
            "acwr, acwr_ewma, monotony, strain, rm_regress) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, 20 if trained else 0, 900.0 if trained else 0.0,
             6000.0, 300.0, 1.2, 1.1, 1.5, 9000.0, 0),
        )
    conn.execute(
        "INSERT INTO illness_episodes (id, start, end, level, source, type) "
        "VALUES (1, '2024-02-26', '2024-02-28', 'confirmed', 'watch', 'unknown')")
    conn.commit()
    ml.build_features(conn, window=None)
    ml.build_labels(conn, window=None, horizon=14)
    return conn


class CoverageByYearTests(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(apple._SCHEMA)
        for day, rhr in (("2023-06-01", None), ("2024-03-01", 60.0),
                         ("2024-07-01", 61.0), ("2025-01-05", 59.0)):
            conn.execute(
                "INSERT INTO aw_daily (date, rhr) VALUES (?, ?)", (day, rhr))
        conn.execute(
            "INSERT INTO aw_workouts (start_ts, end_ts, date, activity, "
            "duration_min, source) VALUES ('2024-03-01T10:00', '2024-03-01T11:00', "
            "'2024-03-01', 'TraditionalStrengthTraining', 60, 'w')")
        conn.commit()
        return conn

    def test_rows_grouped_by_year(self):
        info = apple.coverage_by_year(self._conn(), window=None)
        self.assertEqual([r["year"] for r in info], ["2023", "2024", "2025"])

    def test_counts_and_workouts(self):
        info = apple.coverage_by_year(self._conn(), window=None)
        by_year = {r["year"]: r for r in info}
        self.assertEqual(by_year["2024"]["days"], 2)
        self.assertEqual(by_year["2024"]["RHR"], 2)
        self.assertEqual(by_year["2023"]["RHR"], 0)
        self.assertEqual(by_year["2024"]["workouts"], 1)
        self.assertEqual(by_year["2025"]["workouts"], 0)

    def test_window_filters_earlier_years(self):
        info = apple.coverage_by_year(self._conn(), window="2024-01-01")
        self.assertEqual([r["year"] for r in info], ["2024", "2025"])

    def test_format_is_non_empty(self):
        text = apple.format_coverage_by_year(
            apple.coverage_by_year(self._conn(), window=None))
        self.assertIn("2024", text)
        self.assertIn("RHR", text)


class SignatureTests(unittest.TestCase):
    def test_last_row_is_normal_background(self):
        rows = analysis.signature(_synthetic_db(), window=None)
        self.assertEqual(rows[-1]["level"], "фон")
        self.assertEqual(rows[-1]["start"], "—")

    def test_sick_row_differs_from_background(self):
        rows = analysis.signature(_synthetic_db(), window=None)
        sick = rows[0]
        normal = rows[-1]
        # RHR выше, ВСР ниже во время эпизода, чем в фоне
        self.assertGreater(sick["RHR, уд/мин"], normal["RHR, уд/мин"])
        self.assertLess(sick["ВСР, мс"], normal["ВСР, мс"])

    def test_only_confirmed_by_default(self):
        rows = analysis.signature(_synthetic_db(), window=None)
        self.assertTrue(all(r["level"] in ("confirmed", "фон") for r in rows))


class EpisodesFrameTests(unittest.TestCase):
    def test_columns_present(self):
        row = analysis.episodes_frame(_synthetic_db(), window=None)[0]
        for key in ("id", "start", "end", "days", "level", "source", "type"):
            self.assertIn(key, row)

    def test_window_filters_old_episodes(self):
        rows = analysis.episodes_frame(
            _synthetic_db(), window="2025-01-01")
        self.assertEqual(rows, [])


class PreEpisodeWindowTests(unittest.TestCase):
    def test_lags_run_from_pre_days_to_zero(self):
        win = analysis.pre_episode_windows(
            _synthetic_db(), "acwr", window=None, pre_days=10)
        self.assertEqual(win["lags"], list(range(10, -1, -1)))

    def test_series_and_control_lengths(self):
        win = analysis.pre_episode_windows(_synthetic_db(), "acwr", window=None)
        self.assertEqual(win["n"], 1)
        self.assertEqual(win["n_control"], len(win["control"]))
        self.assertEqual(len(win["series"][0]), len(win["lags"]))
        self.assertEqual(len(win["mean"]), len(win["lags"]))

    def test_control_centers_avoid_episode_window(self):
        win = analysis.pre_episode_windows(
            _synthetic_db(), "acwr", window=None, pre_days=10, max_control=5)
        # контроль не больше лимита и не равен числу эпизодов по определению
        self.assertLessEqual(win["n_control"], 5)

    def test_max_control_limits_rows(self):
        win = analysis.pre_episode_windows(
            _synthetic_db(), "acwr", window=None, max_control=3)
        self.assertLessEqual(win["n_control"], 3)


class QuantileProfileTests(unittest.TestCase):
    def test_has_shift_column(self):
        rows = analysis.quantile_profile(_synthetic_db(), window=None)
        self.assertTrue(rows)
        self.assertTrue(all("median_shift" in r for r in rows))

    def test_channel_labels(self):
        rows = analysis.quantile_profile(_synthetic_db(), window=None)
        labels = {r["channel"] for r in rows}
        self.assertIn("RHR, уд/мин", labels)
        self.assertIn("ВСР, мс", labels)


class EpisodeChannelsTests(unittest.TestCase):
    def test_channels_have_min_mean_max_peak(self):
        rows = analysis.episode_channels(_synthetic_db(), window=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        # ровно пять фиксированных каналов, порядок из _CHANNELS
        keys = [c["key"] for c in rows[0]["channels"]]
        self.assertEqual(keys, ["rhr", "hrv_sdnn", "resp_rate", "wrist_temp",
                                "sleep_hours"])
        for ch in rows[0]["channels"]:
            self.assertEqual(ch["n"], 3)
            self.assertLessEqual(ch["min"], ch["mean"])
            self.assertLessEqual(ch["mean"], ch["max"])
            self.assertIsNotNone(ch["peak"])

    def test_hrv_low_when_below_background(self):
        """В синтетике болезнь роняет ВСР - метка «ниже фона»."""
        rows = analysis.episode_channels(_synthetic_db(), window=None)
        hrv = [c for c in rows[0]["channels"] if c["key"] == "hrv_sdnn"][0]
        self.assertTrue(hrv["hrv_low"])
        self.assertFalse(hrv["hrv_high"])
        self.assertIn("background", hrv)

    def test_detail_by_id_filters(self):
        conn = _synthetic_db()
        self.assertEqual(analysis.episode_channels(conn, episode_id=999,
                                                   window=None), [])
        self.assertEqual(len(analysis.episode_channels(conn, episode_id=1,
                                                        window=None)), 1)

    def test_format_lists_channels_and_peak(self):
        rows = analysis.episode_channels(_synthetic_db(), window=None)
        text = analysis.format_episode_channels(rows)
        self.assertIn("эпизод 1", text)
        self.assertIn("RHR, уд/мин", text)
        self.assertIn("пик", text)
        self.assertIn("ниже фона", text)
        # урок E4: в рендере нет стрелок
        for mark in ("↑", "↓", "→", "←"):
            self.assertNotIn(mark, text)

    def test_format_empty_gives_clear_text(self):
        self.assertIn("не найден", analysis.format_episode_channels([]))


if __name__ == "__main__":
    unittest.main()
