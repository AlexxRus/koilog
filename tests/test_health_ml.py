"""Тесты ML-слоя: ``koilog.health.ml``.

Прогон на маленькой синтетической базе в памяти. Если ``numpy``/``scikit-learn``
не установлены - модельные тесты пропускаются, а проверка «ядро не тянет ML»
и тест на отсутствие обратного импорта остаются.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog.health import apple, illness, load, ml


def _calendar(start: str, count: int) -> list[str]:
    first = date_cls.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _synthetic_db(days: int = 80) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(apple._SCHEMA)
    conn.executescript(load._SCHEMA)
    conn.executescript(illness._SCHEMA)
    conn.executescript(ml._FEATURE_SCHEMA)

    calendar = _calendar("2024-01-01", days)
    for index, day in enumerate(calendar):
        # простая физиология: чуть варьирующий фон, «болезнь» около 55-го дня
        sick = 55 <= index <= 60
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
    conn.commit()
    return conn


class FeatureTests(unittest.TestCase):
    def test_features_are_built_per_day(self):
        conn = _synthetic_db()
        count = ml.build_features(conn, window=None)
        self.assertEqual(count, 80)
        row = conn.execute(
            "SELECT * FROM ml_features WHERE date = '2024-02-25'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["z_rhr"])
        self.assertGreater(row["sleep_debt7"], 0.0)

    def test_labels_horizon(self):
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        positives = ml.build_labels(conn, window=None, horizon=14)
        self.assertEqual(positives, 14)
        # день за 15 дней до эпизода уже не размечен
        self.assertEqual(
            conn.execute("SELECT y FROM ml_features WHERE date = '2024-02-11'")
            .fetchone()["y"], 0)
        self.assertEqual(
            conn.execute("SELECT episode_id FROM ml_features WHERE date = '2024-02-25'")
            .fetchone()["episode_id"], 1)

    def test_gap_only_not_a_positive(self):
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-03-01', 'gap_only', 'gap', 'unknown')")
        conn.commit()
        self.assertEqual(ml.build_labels(conn, window=None), 0)


@unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
class ModelTests(unittest.TestCase):
    def test_leave_one_episode_out_runs(self):
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        ml.build_labels(conn, window=None)
        result = ml.leave_one_episode_out(conn, ml.MODEL_FEATURES, model="logreg")
        self.assertEqual(result["episodes"], 1)
        self.assertIn("episode_recall", result)
        self.assertIn("false_positives_per_month", result)

    def test_report_assembles_blocks(self):
        conn = _synthetic_db()
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        data = ml.report(conn, window=None, as_json=True)
        self.assertIn("cv_logreg", data)
        self.assertIn("lag_tonnage", data)
        self.assertIn("red_flag", data)
        import json
        self.assertIsInstance(json.loads(ml.dumps(data)), dict)


class IsolationTests(unittest.TestCase):
    def test_core_health_modules_do_not_pull_ml(self):
        code = (
            "import sys\n"
            "import koilog.health.apple, koilog.health.load, koilog.health.illness\n"
            "import koilog.health.analysis, koilog.health.text\n"
            "bad = [m for m in ('numpy', 'sklearn') if m in sys.modules]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_core_does_not_import_health(self):
        # обратной связи быть не должно: ядро koilog не знает про health.
        # cli.py - это диспетчер команд, ему health нужен; проверяем именно ядро.
        code = (
            "import sys\n"
            "import koilog.core, koilog.taxonomy, koilog.equipment, "
            "koilog.rules, koilog.etl, koilog.stats\n"
            "bad = [m for m in sys.modules if m.startswith('koilog.health')]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_ml_module_is_importable(self):
        # импорт ml.py не должен падать, даже если numpy/sklearn отсутствуют
        self.assertTrue(callable(ml.build_features))
        self.assertIsInstance(ml.HAS_ML, bool)


if __name__ == "__main__":
    unittest.main()
