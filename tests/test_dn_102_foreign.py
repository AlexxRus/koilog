"""Пины флага ``is_foreign`` и фильтра чужих тренировок (docs/dn-102-prereg.md).

Группы проверок:

* **вычисление флага** - ``_finalize`` помечает ``program='Диана'`` (п.D1-бис) и
  защитный ``workout_name LIKE '%Диана%'``; чужие программы (Raketa и т.п.) - 0;
* **схема и миграция** - колонка ``is_foreign`` есть и дефолт 0; ``_add_missing_columns``
  доводит старую таблицу (п.T1/T4);
* **перезагрузка отчёта** - ``load_workoutreport`` ставит флаг, ``upsert`` пишет его,
  а ``DELETE`` + повторный ``upsert`` (переэкспорт отчёта) флаг не теряет (п.T2/T4);
* **фильтры витрин** - ``window_sets``, ``recent_exercises``, ``history``,
  ``weekly_koi`` (вью `v_weekly_koi`), ``muscle_load.ring`` не видят foreign-сеты (п.T5);
* **раздел качества** - ``quality_report`` печатает срез чужих тренировок (п.T6);
* **фенс health** - ``health/load.py`` и решающие модули не читают ``is_foreign`` (п.§5/§6).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from koilog import etl, muscle_load, stats

ROOT = Path(__file__).resolve().parents[1]


#: Минимальный отчёт с чужой программой «Диана» и своей «Raketa».
REPORT_SAMPLE = """Всего тренировок;2
;;Вес;;Пвт

Дата;23 сент. 2021 г.
Программа;Диана
День;день 1
Продолжительность;21 мин.

;1. Жим штанги лежа
;;60.0;x;5

Дата;25 сент. 2021 г.
Программа;Raketa
День;День 2
Продолжительность;1 ч 5 мин.

;1. Жим штанги лежа
;;80.0;x;5
"""


class FlagComputationTests(unittest.TestCase):
    """п.D1-бис: канонический и защитный предикаты вычисляются в ``_finalize``."""

    def _row(self, *, program=None, workout_name=None):
        return etl.SetRow(
            source="report", source_row=1, session_id="s", date="2021-09-23",
            exercise="Жим штанги лежа", weight=60.0, reps=5,
            program=program, workout_name=workout_name,
        )

    def test_program_diana_is_foreign(self):
        row = etl._finalize(self._row(program="Диана"))
        self.assertEqual(row.is_foreign, 1)

    def test_workout_name_like_diana_is_foreign_without_program(self):
        row = etl._finalize(self._row(workout_name="Диана / день 1"))
        self.assertEqual(row.is_foreign, 1)

    def test_other_program_is_not_foreign(self):
        row = etl._finalize(self._row(program="Raketa",
                                      workout_name="Raketa / День 2"))
        self.assertEqual(row.is_foreign, 0)

    def test_default_is_zero(self):
        row = etl._finalize(self._row())
        self.assertEqual(row.is_foreign, 0)


class SchemaTests(unittest.TestCase):
    def test_column_exists_with_default_zero(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        etl.create_schema(conn)
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(sets)")}
        self.assertIn("is_foreign", columns)
        self.assertEqual(columns["is_foreign"][4], "0")

    def test_add_missing_columns_backfills_the_flag(self):
        """п.T4: миграция доводит существующую таблицу, не пересоздавая её."""
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE sets (source TEXT, source_row INTEGER)")
        etl._add_missing_columns(conn)
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(sets)")}
        self.assertIn("is_foreign", columns)
        self.assertEqual(columns["is_foreign"][4], "0")


class ReportParserForeignTests(unittest.TestCase):
    def test_diana_rows_are_foreign_and_raketa_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.txt"
            path.write_text(REPORT_SAMPLE, encoding="utf-8")
            rows = list(etl.load_workoutreport(path))
        diana = [r for r in rows if r.program == "Диана"]
        raketa = [r for r in rows if r.program == "Raketa"]
        self.assertTrue(diana)
        self.assertTrue(raketa)
        self.assertTrue(all(r.is_foreign == 1 for r in diana))
        self.assertTrue(all(r.is_foreign == 0 for r in raketa))


class ReloadSurvivalTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        etl.create_schema(self.conn)
        self.addCleanup(self.conn.close)

    def test_report_reload_keeps_the_flag(self):
        """п.T2/T4: флаг переживает DELETE + повторный INSERT отчёта."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.txt"
            path.write_text(REPORT_SAMPLE, encoding="utf-8")
            rows = list(etl.load_workoutreport(path))
        etl.upsert(self.conn, rows)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sets WHERE program = 'Диана' AND is_foreign = 1"
            ).fetchone()[0],
            1,
        )
        # Переэкспорт отчёта: старый источник стирается, строки вставляются заново.
        self.conn.execute("DELETE FROM sets WHERE source = ?", (etl.SOURCE_REPORT,))
        self.conn.commit()
        etl.upsert(self.conn, rows)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sets WHERE program = 'Диана' AND is_foreign = 1"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0],
            2,
        )


class ViewportFilterTests(unittest.TestCase):
    """п.T5: витрины считают только своё - foreign-сеты исключены."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        etl.create_schema(self.conn)
        self.addCleanup(self.conn.close)

    def _insert(self, row_id, exercise, *, is_foreign=0, date="2024-01-10",
                weight=100.0, reps=5, **kw):
        base = {
            "source": "journal", "source_row": row_id,
            "session_id": f"s{row_id}", "date": date,
            "exercise": exercise, "exercise_group": "ГРУДЬ",
            "set_index": 1, "weight": weight, "reps": reps,
            "is_foreign": is_foreign,
        }
        base.update(kw)
        columns = ", ".join(base)
        marks = ", ".join("?" * len(base))
        self.conn.execute(
            f"INSERT INTO sets ({columns}) VALUES ({marks})", tuple(base.values())
        )
        self.conn.commit()

    def test_window_sets_excludes_foreign(self):
        self._insert(1, "Жим штанги лежа", is_foreign=0)
        self._insert(2, "Жим штанги лежа", is_foreign=1)
        total, working = stats.window_sets(self.conn)
        self.assertEqual(total, 1)
        self.assertEqual(working, 1)

    def test_recent_exercises_excludes_foreign(self):
        self._insert(1, "Жим штанги лежа", is_foreign=0)
        self._insert(2, "Разгибание руки с гантелью из-за головы", is_foreign=1)
        names = {r.exercise for r in stats.recent_exercises(self.conn)}
        self.assertIn("Жим штанги лежа", names)
        self.assertNotIn("Разгибание руки с гантелью из-за головы", names)

    def test_history_excludes_foreign_session(self):
        self._insert(1, "Жим штанги лежа", is_foreign=0, rm=125.0,
                     rm_session=125.0)
        self._insert(2, "Жим штанги лежа", is_foreign=1, rm=13.0, rm_session=13.0,
                     weight=10.0)
        rows = stats.history(self.conn, "Жим штанги лежа")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, "s1")

    def test_weekly_koi_excludes_foreign(self):
        self._insert(1, "Жим штанги лежа", is_foreign=0, koi=2.0,
                     exercise_group="ГРУДЬ")
        self._insert(2, "Разгибание руки с гантелью из-за головы", is_foreign=1,
                     koi=1.0, exercise_group="ТРИЦЕПС")
        rows = stats.weekly_koi(self.conn)
        self.assertEqual({r.exercise for r in rows}, {"Жим штанги лежа"})

    def test_muscle_ring_excludes_foreign(self):
        self._insert(1, "Жим штанги лежа", is_foreign=0)
        self._insert(2, "Жим штанги лежа", is_foreign=1)
        self.assertEqual(muscle_load.ring(self.conn).sets, 1)


class QualityReportForeignTests(unittest.TestCase):
    def test_foreign_section_is_rendered(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        etl.create_schema(conn)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise, "
            "exercise_group, set_index, weight, reps, is_foreign, is_outlier, koi) "
            "VALUES ('report', 1, 's1', '2021-09-08', 'Жим штанги лежа', 'ГРУДЬ', "
            "1, 60.0, 5, 1, 0, 1.0)"
        )
        conn.commit()
        report = etl.quality_report(conn)
        self.assertIsNotNone(report.foreign)
        self.assertEqual(report.foreign.sets, 1)
        self.assertEqual(report.foreign.days, 1)
        text = report.format(limit=5)
        self.assertIn("ЧУЖИЕ ТРЕНИРОВКИ", text)
        self.assertIn("исключены из витрин, данные сохранены (append-only)", text)

    def test_no_foreign_rows_gives_no_section(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        etl.create_schema(conn)
        self.addCleanup(conn.close)
        report = etl.quality_report(conn)
        self.assertIsNone(report.foreign)
        self.assertNotIn("ЧУЖИЕ ТРЕНИРОВКИ", report.format(limit=5))


class HealthFenceTests(unittest.TestCase):
    """п.§5/§6: health-конвейер читает ``sets`` без знания ``is_foreign``."""

    def test_health_load_does_not_filter_foreign(self):
        text = (ROOT / "koilog" / "health" / "load.py").read_text(encoding="utf-8")
        self.assertNotIn("is_foreign", text)

    def test_decision_modules_do_not_mention_foreign(self):
        for name in ("state.py", "retrain.py", "groups.py", "digest.py",
                     "score.py", "ml.py"):
            text = (ROOT / "koilog" / "health" / name).read_text(encoding="utf-8")
            self.assertNotIn("is_foreign", text, name)

    def test_metrics_path_is_not_touched(self):
        # Пин §6: recompute_metrics (rm_session/koi) не чистит чужие строки,
        # иначе полетел бы health. Флаг только в витринах.
        self.assertNotIn("is_foreign", etl._COUNTS_SQL)


if __name__ == "__main__":
    unittest.main()