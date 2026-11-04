"""RW-98: восходящая дуга относительной работы (display-only).

Пререг: ``docs/rw-98-prereg.md``. Проверяет контракт §4/§6:
детерминизм, пин ``R = ΣE×reps / BW(год)``, дельту через разрыв без шитья,
первую неделю без дельты, OR-стоп по отдельности, отсутствие литерала ``1.5``,
поверхности (группа/упражнение/пусто), ISO-стык года, import-фенс и золотой пин
строки таблицы.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from koilog import etl, taxonomy, work_arc

ROOT = Path(__file__).resolve().parent.parent


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            date         TEXT,
            session_id   TEXT,
            exercise     TEXT,
            weight       REAL,
            reps         INTEGER,
            weight_mode  TEXT,
            exercise_group TEXT,
            koi          REAL,
            is_warmup    INTEGER NOT NULL DEFAULT 0,
            skipped      INTEGER NOT NULL DEFAULT 0,
            is_outlier   INTEGER NOT NULL DEFAULT 0,
            is_foreign   INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.executescript(etl.WEEKLY_KOI_VIEW)
    conn.executescript(
        """
        CREATE TABLE load_daily (
            date         TEXT PRIMARY KEY,
            acwr_warmup  REAL,
            gap_warmup   INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return conn


def _add(conn, date, exercise, weight, reps=10, group=None, koi=None) -> None:
    if group is None:
        group = taxonomy.group_of(exercise).name
    conn.execute(
        "INSERT INTO sets (date, session_id, exercise, weight, reps, "
        "weight_mode, exercise_group, koi) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (date, "s1", exercise, weight, reps, "absolute", group, koi),
    )


def _acwr(conn, date, value, gap_warmup=0) -> None:
    conn.execute(
        "INSERT INTO load_daily (date, acwr_warmup, gap_warmup) "
        "VALUES (?, ?, ?)",
        (date, value, gap_warmup),
    )


class WeekOfTests(unittest.TestCase):
    def test_iso_week_boundary_is_one_week_not_w00(self):
        # 29-31.12 и 1-3.01 — одна неделя 2026-W01, а не разрыв на W52/W00.
        self.assertEqual(work_arc.week_of("2025-12-29"), "2026-W01")
        self.assertEqual(work_arc.week_of("2026-01-01"), "2026-W01")
        self.assertEqual(work_arc.week_of("2026-01-03"), "2026-W01")
        self.assertNotIn("W00", work_arc.week_of("2026-01-03"))
        self.assertEqual(work_arc.week_of("2024-01-10"), "2024-W02")


class TonnagePinTests(unittest.TestCase):
    def test_r_is_tonnage_over_bodyweight(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10)
        _add(conn, "2024-01-08", "Жим штанги лежа", 50, 20)
        conn.commit()
        # 100*10 + 50*20 = 2000 кг по группе ЖИМ ЛЁЖА
        self.assertEqual(
            work_arc.weekly_tonnage(conn, group="ЖИМ ЛЁЖА"),
            {"2024-W02": 2000.0},
        )
        # BW года неизвестен -> фоллбэк BODY_WEIGHT = 70.0; R = 2000/70.
        points = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].tonnage, 2000.0)
        self.assertEqual(points[0].bodyweight, 70.0)
        self.assertAlmostEqual(points[0].r, 2000.0 / 70.0)
        self.assertIsNone(points[0].delta)

    def test_exercise_selector_narrows_surface(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10)
        _add(conn, "2024-01-08", "Становая тяга", 200, 10)
        conn.commit()
        points = work_arc.arc(conn, exercise="Жим штанги лежа")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].surface, "Жим штанги лежа")
        self.assertEqual(points[0].tonnage, 1000.0)


class ArcLogicTests(unittest.TestCase):
    def test_first_week_has_no_delta_and_gap_is_annotated_not_stitched(self):
        conn = _db()
        self.addCleanup(conn.close)
        # W01 и W05: между ними пустые недели W02-W04 (разрыв поверхности).
        _add(conn, "2024-01-03", "Жим штанги лежа", 100, 10)
        _add(conn, "2024-01-29", "Жим штанги лежа", 200, 10)
        _acwr(conn, "2024-01-29", 1.2, gap_warmup=1)
        conn.commit()
        points = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        self.assertEqual([p.week for p in points], ["2024-W01", "2024-W05"])
        self.assertIsNone(points[0].delta)
        self.assertFalse(points[0].after_gap)
        # дельта прыгает через разрыв (не шьётся), но аннотируется
        self.assertIsNotNone(points[1].delta)
        self.assertAlmostEqual(points[1].delta, (2000 - 1000) / 70.0)
        self.assertTrue(points[1].after_gap)


class StopTests(unittest.TestCase):
    def test_acwr_stop_alone(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10)
        _acwr(conn, "2024-01-08", 1.8)  # > ACWR_ALERT (1.5)
        conn.commit()
        (point,) = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        self.assertTrue(point.acwr_stop)
        self.assertFalse(point.koi_stop)
        self.assertTrue(point.stop)

    def test_koi_stop_alone(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10, koi=3.5)
        conn.commit()  # SUM(koi) = 3.5 -> «Очень тяжёлый» (>= полоса 3)
        (point,) = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        self.assertFalse(point.acwr_stop)
        self.assertTrue(point.koi_stop)
        self.assertTrue(point.stop)
        self.assertEqual(point.koi_band, "Очень тяжёлый")

    def test_no_stop_when_signals_below_threshold(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10, koi=1.0)
        _acwr(conn, "2024-01-08", 1.0)
        conn.commit()
        (point,) = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        self.assertFalse(point.acwr_stop)
        self.assertFalse(point.koi_stop)
        self.assertFalse(point.stop)

    def test_both_signals_ignite_or_stop(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10, koi=4.5)
        _acwr(conn, "2024-01-08", 2.0)
        conn.commit()
        (point,) = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        self.assertTrue(point.acwr_stop)
        self.assertTrue(point.koi_stop)
        self.assertTrue(point.stop)


class SurfaceTests(unittest.TestCase):
    def test_empty_and_missing_surfaces(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10)
        conn.commit()
        self.assertEqual(work_arc.arc(conn, group="ПРЕСС"), [])
        self.assertEqual(work_arc.arc(conn), [])
        self.assertEqual(work_arc.format_arc([]), "нет данных\n")

    def test_group_and_exercise_are_mutually_exclusive(self):
        conn = _db()
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError):
            work_arc.arc(conn, group="ЖИМ ЛЁЖА", exercise="Жим штанги лежа")
        with self.assertRaises(ValueError):
            work_arc.weekly_tonnage(conn, group="ЖИМ ЛЁЖА",
                                    exercise="Жим штанги лежа")

    def test_surfaces_lists_only_nonzero(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-08", "Жим штанги лежа", 100, 10)
        conn.commit()
        groups, exercises = work_arc.surfaces(conn)
        self.assertEqual(groups, ["ЖИМ ЛЁЖА"])
        self.assertEqual(exercises, ["Жим штанги лежа"])


class DeterminismTests(unittest.TestCase):
    def test_two_runs_are_bit_identical(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-03", "Жим штанги лежа", 100, 10)
        _add(conn, "2024-01-08", "Жим штанги лежа", 120, 10, koi=3.5)
        _acwr(conn, "2024-01-08", 1.8)
        conn.commit()
        first = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        second = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        self.assertEqual(first, second)
        self.assertEqual(work_arc.format_arc(first), work_arc.format_arc(second))


class GoldenPinTests(unittest.TestCase):
    def test_format_arc_matches_golden_string(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-03", "Жим штанги лежа", 100, 10)
        _add(conn, "2024-01-08", "Жим штанги лежа", 120, 10)
        conn.commit()
        points = work_arc.arc(conn, group="ЖИМ ЛЁЖА")
        expected = (
            "относительная работа недели: ЖИМ ЛЁЖА (2 недель)\n"
            "неделя      R      Δ R     ACWR     КОИ             разрыв   стоп\n"
            "----------------------------------------------------------------------\n"
            "2024-W01      14.3       —      —   —\n"
            "2024-W02      17.1    +2.9      —   —\n"
        )
        self.assertEqual(work_arc.format_arc(points), expected)


class FenceTests(unittest.TestCase):
    def test_no_literal_1_5(self):
        source = (ROOT / "koilog" / "work_arc.py").read_text(encoding="utf-8")
        self.assertNotIn("1.5", source)
        self.assertIn("ACWR_ALERT", source)

    def test_decision_modules_do_not_import_work_arc(self):
        files = [
            ROOT / "koilog" / "health" / name
            for name in ("state.py", "retrain.py", "digest.py", "score.py",
                         "ml.py")
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("work_arc", text, path.name)

    def test_importing_work_arc_does_not_pull_decision_modules(self):
        code = (
            "import sys\n"
            "import koilog.work_arc\n"
            "bad = [m for m in sys.modules if m in "
            "('koilog.health.state', 'koilog.health.score', "
            "'koilog.health.ml')]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()