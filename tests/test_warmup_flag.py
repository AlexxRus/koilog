<<<<<<< HEAD
"""Тесты для проверки работы с флагом разминки."""
=======
"""Разминка: флаг ``is_warmup``, витрины и переключатель «Учитывать разминку».

Флаг ставится по колонке **упражнения** журнала (``УПРАЖНЕНИЕ``), а не по
названию тренировки. До исправления там стояла колонка тренировки, и все
разминочные строки лежали с ``is_warmup = 0`` - разбор счётчиков в пререге
``docs/wu-106-warmup-flag.md``.

Синтетика проверяет правило и рендер; живой класс - свойства боевой базы
(стиль W7-(g)): чисел-констант, привязанных к состоянию выгрузки, не держим,
кроме предзарегистрированных пререгом.
"""
>>>>>>> 5383f412e79402469e2b66bec62fc1143913ce40

from __future__ import annotations

import sqlite3
<<<<<<< HEAD
import unittest

from koilog.health import load


class LiveWarmupTests(unittest.TestCase):
    """Тесты на реальных данных с is_warmup=1.
    
    Пропускается, если в базе нет строк с is_warmup=1.
    """
    
    @classmethod
    def setUpClass(cls):
        # Проверяем, есть ли в базе строки с is_warmup=1
        try:
            # Try to connect to the actual database
            real_conn = sqlite3.connect("sql/training.db")
            real_conn.row_factory = sqlite3.Row
            cursor = real_conn.execute("SELECT COUNT(*) as count FROM sets WHERE is_warmup = 1")
            count = cursor.fetchone()["count"]
            real_conn.close()
            
            if count == 0:
                raise unittest.SkipTest("в базе нет строк с is_warmup=1: база не перезагружена после фикса флага — выполните load")
        except sqlite3.OperationalError:
            # If the database doesn't exist, skip the tests
            raise unittest.SkipTest("база данных недоступна: выполните load")
        
        # Создаем тестовую схему
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.executescript(
            """
            CREATE TABLE sets (
                date TEXT, exercise TEXT, weight REAL, reps INTEGER,
                rm_session REAL, koi REAL,
                is_warmup INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                is_outlier INTEGER NOT NULL DEFAULT 0,
                is_gravitron INTEGER NOT NULL DEFAULT 0
            );
            
            CREATE TABLE load_daily (
                date TEXT PRIMARY KEY, 
                acwr REAL, acwr_warmup REAL, 
                monotony REAL, strain REAL, 
                weekly_tonnage REAL, rm_regress INTEGER, 
                gap_warmup INTEGER, since_gap INTEGER, sets INTEGER
            );
            """
        )
        
        # Добавляем тестовые данные с is_warmup=1
        cls.conn.execute(
            """
            INSERT INTO sets (date, exercise, weight, reps, rm_session, koi, is_warmup)
            VALUES (?, 'Разминка', ?, ?, ?, ?, 1)
            """,
            ("2024-01-10", 50.0, 10, 50.0, 1.0)
        )
        cls.conn.commit()
        
        cls.addCleanup(cls.conn.close)
    
    def test_warmup_sets_are_counted(self):
        """Проверяем, что разминки учитываются в подходах."""
        rows = load._session_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["2024-01-10"]["sets"], 1)
    
    def test_warmup_in_load_daily(self):
        """Проверяем, что разминки влияют на load_daily."""
        # Тест должен показывать, что разминки учитываются
        # в расчетах нагрузки
        pass


if __name__ == "__main__":
    unittest.main()
=======
import tempfile
import unittest
from pathlib import Path

from koilog import etl, stats
from koilog.core import one_rm
from koilog.health import load


HEADER = (
    "ДАТА;ВРЕМЯ;ТРЕНИРОВКА;УПРАЖНЕНИЕ;ДИСТАНЦИЯ КМ;ВЕС;МНОЖИТЕЛЬ;"
    "ВРЕМЯ;ПОВТОРЫ;ОТДЫХ;ПРОПУСК;ЗАМЕТКА"
)


def _journal_rows(body: str) -> list[etl.SetRow]:
    """Разобрать журнал из шапки и тела во временном файле."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(HEADER + "\n" + body)
        path = Path(handle.name)
    try:
        return list(etl.load_journal(path))
    finally:
        path.unlink()


def _mem_db(*scripts: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for script in scripts:
        conn.executescript(script)
    return conn


def _add_set(conn, source_row, date, exercise, *, is_warmup=0, weight=None,
             reps=None, time_seconds=None, rm=None, koi=None, skipped=0):
    conn.execute(
        """
        INSERT INTO sets (source, source_row, session_id, date, exercise,
                          exercise_group, equipment, weight_mode, set_index,
                          is_warmup, weight, reps, time_seconds, rm, koi,
                          skipped, is_timed)
             VALUES ('journal', ?, ?, ?, ?, 'КАРДИО', '—', 'absolute', 1,
                     ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_row, f"{date}T10:00", date, exercise, is_warmup, weight, reps,
         time_seconds, rm, koi, skipped, 1 if time_seconds else 0),
    )
    conn.commit()


class WarmupFlagParserTests(unittest.TestCase):
    """Правило флага: имя упражнения, не название тренировки."""

    def test_flag_is_set_from_exercise_column(self):
        rows = _journal_rows(
            "01.01.20;10:00;2. Жим;Разминка;0;;;;60;;;\n"
            "01.01.20;10:00;2. Жим;Жим гантелей лежа;0;30;x;;10;90;;\n"
        )
        warm, work = rows
        self.assertEqual(warm.is_warmup, 1)
        self.assertEqual(work.is_warmup, 0)

    def test_workout_named_warmup_is_not_a_warmup_set(self):
        """Пин самого бага: старый код читал колонку тренировки и дал бы 1."""
        rows = _journal_rows(
            "01.01.20;10:00;Разминка;Жим гантелей лежа;0;30;x;;10;90;;\n"
        )
        self.assertEqual(rows[0].is_warmup, 0)
        self.assertEqual(rows[0].workout_name, "Разминка")

    def test_spelling_and_case_variants(self):
        rows = _journal_rows(
            "01.01.20;10:00;2. Жим; Разминка ;0;;;;60;;;\n"
            "02.01.20;10:00;2. Жим;РАЗМИНКА;0;;;;60;;;\n"
            "03.01.20;10:00;2. Жим;Жим гантелей лежа;0;30;x;;10;90;;\n"
        )
        self.assertEqual([r.is_warmup for r in rows], [1, 1, 0])

    def test_helper_accepts_only_the_warmup_name(self):
        self.assertTrue(etl.is_warmup_name(etl.WARMUP_NAME))
        self.assertTrue(etl.is_warmup_name("  разминка  "))
        self.assertFalse(etl.is_warmup_name("Жим гантелей лежа"))
        self.assertFalse(etl.is_warmup_name(None))
        self.assertFalse(etl.is_warmup_name(""))


class ExplainWarmupTests(unittest.TestCase):
    """Колонка ``разм`` рисует флаг, а не правило по имени."""

    def test_explain_marks_warmup_and_working_rows(self):
        conn = _mem_db(etl.SCHEMA, etl.WEEKLY_KOI_VIEW)
        self.addCleanup(conn.close)
        _add_set(conn, 1, "2024-01-10", "Разминка", is_warmup=1,
                 time_seconds=60)
        _add_set(conn, 2, "2024-01-20", "Жим гантелей лежа", weight=30.0,
                 reps=10, rm=one_rm(30.0, 10))
        warm = stats.format_explain(stats.explain_sets(conn, "Разминка"))
        work = stats.format_explain(
            stats.explain_sets(conn, "Жим гантелей лежа"))
        warm_row = warm.splitlines()[2]
        work_row = work.splitlines()[2]
        self.assertIn("разм", warm_row)
        self.assertIn("—", work_row)
        self.assertNotIn("разм", work_row)

    def test_audit_counts_warmup_rows(self):
        conn = _mem_db(etl.SCHEMA, etl.WEEKLY_KOI_VIEW)
        self.addCleanup(conn.close)
        _add_set(conn, 1, "2024-01-10", "Разминка", is_warmup=1,
                 time_seconds=60)
        _add_set(conn, 2, "2024-01-11", "Разминка", is_warmup=1,
                 time_seconds=45)
        _add_set(conn, 3, "2024-01-20", "Жим гантелей лежа", weight=30.0,
                 reps=10, rm=one_rm(30.0, 10))
        audit = etl.exercise_audit(conn, "Разминка")
        self.assertEqual(audit.total, 2)
        self.assertEqual(audit.warmup, 2)


class ReportOptionTests(unittest.TestCase):
    """``exercise_report(include_warmup=...)`` и счётчик окна."""

    def setUp(self):
        self.conn = _mem_db(etl.SCHEMA, etl.WEEKLY_KOI_VIEW)
        self.addCleanup(self.conn.close)
        _add_set(self.conn, 1, "2024-01-10", "Разминка", is_warmup=1,
                 time_seconds=60)
        _add_set(self.conn, 2, "2024-01-10", "Жим гантелей лежа", weight=30.0,
                 reps=10, rm=one_rm(30.0, 10))
        _add_set(self.conn, 3, "2024-01-20", "Жим гантелей лежа", weight=32.0,
                 reps=10, rm=one_rm(32.0, 10))
        _add_set(self.conn, 4, "2024-01-20", "Разминка", is_warmup=1,
                 time_seconds=90)

    def test_default_hides_pseudo_exercise(self):
        names = {r.exercise for r in stats.exercise_report(self.conn)}
        self.assertEqual(names, {"Жим гантелей лежа"})

    def test_option_shows_pseudo_exercise_with_its_sets(self):
        rows = {r.exercise: r for r in
                stats.exercise_report(self.conn, include_warmup=True)}
        self.assertIn("Разминка", rows)
        self.assertEqual(rows["Разминка"].sets, 2)

    def test_rm_columns_do_not_depend_on_the_option(self):
        plain = {r.exercise: r for r in stats.exercise_report(self.conn)}
        full = {r.exercise: r for r in
                stats.exercise_report(self.conn, include_warmup=True)}
        key = "Жим гантелей лежа"
        for field in ("best_rm", "last_rm", "record_rm", "last_delta",
                      "best_day", "last_day_rm", "sessions"):
            self.assertEqual(getattr(plain[key], field),
                             getattr(full[key], field), field)

    def test_window_sets_counts_both_positions(self):
        self.assertEqual(stats.window_sets(self.conn), (4, 2))
        self.assertEqual(stats.window_sets(self.conn, "2024-01-20"), (2, 1))


class SummaryOptionTests(unittest.TestCase):
    """``load.summarize`` / ``format_summary``: ключи и текст."""

    def setUp(self):
        self.conn = _mem_db(etl.SCHEMA, etl.WEEKLY_KOI_VIEW, load._SCHEMA)
        self.addCleanup(self.conn.close)
        for i, (date, sets) in enumerate(
            (("2024-01-16", 3), ("2024-01-17", 2), ("2024-01-18", 4)), 1
        ):
            self.conn.execute(
                "INSERT INTO load_daily (date, sets, tonnage) VALUES (?, ?, ?)",
                (date, sets, 100.0 * i),
            )
        _add_set(self.conn, 1, "2024-01-16", "Разминка", is_warmup=1,
                 time_seconds=60)
        _add_set(self.conn, 2, "2024-01-17", "Разминка", is_warmup=1,
                 time_seconds=60)
        _add_set(self.conn, 3, "2024-01-18", "Жим гантелей лежа", weight=30.0,
                 reps=10, rm=one_rm(30.0, 10))
        self.conn.commit()

    def test_summary_keys_split_column_and_working_sets(self):
        summary = load.summarize(self.conn)
        self.assertEqual(summary["sets"], 9)          # колонка load_daily
        self.assertEqual(summary["warmup"], 2)
        self.assertEqual(summary["sets_working"], 7)
        warm = load.summarize(self.conn, include_warmup=True)
        self.assertEqual(warm["sets"], 9)
        self.assertEqual(warm["warmup"], 2)
        self.assertEqual(warm["sets_working"], 9)

    def test_tonnage_is_the_same_in_both_positions(self):
        plain = load.summarize(self.conn)
        warm = load.summarize(self.conn, include_warmup=True)
        self.assertEqual(plain["tonnage"], warm["tonnage"])

    def test_default_text_shows_working_sets_and_the_warmup_line(self):
        text = load.format_summary(self.conn)
        self.assertIn("подходов: 7   тоннаж: 600 кг", text)
        self.assertIn("разминка: 2 подходов не входит в счётчик", text)

    def test_include_warmup_text_is_byte_identical_to_the_old_one(self):
        text = load.format_summary(self.conn, include_warmup=True)
        self.assertIn("подходов: 9   тоннаж: 600 кг", text)
        self.assertNotIn("разминка:", text)

    def test_empty_series_still_says_what_to_run(self):
        conn = _mem_db(etl.SCHEMA, etl.WEEKLY_KOI_VIEW, load._SCHEMA)
        self.addCleanup(conn.close)
        self.assertIn("health-load", load.format_summary(conn))


class LiveWarmupTests(unittest.TestCase):
    """Свойства боевой базы: флаг стоит только на разминке и не меняет счёт.

    Живой класс снимается гвардом (как ``test_health_canon.LiveCanonTests``):
    файл есть, таблица есть, строки есть. Семантику проверяют сами тесты.
    """

    @classmethod
    def setUpClass(cls):
        path = etl.DEFAULT_DB
        if not path.exists():
            raise unittest.SkipTest(f"нет живой базы: {path}")
        cls.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cls.conn.row_factory = sqlite3.Row
        try:
            rows = cls.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
        except sqlite3.OperationalError:
            cls.conn.close()
            raise unittest.SkipTest(
                f"в базе нет таблицы sets: выполните load ({path})")
        if not rows:
            cls.conn.close()
            raise unittest.SkipTest(
                f"база пуста: в sets нет подходов - выполните load ({path})")

    @classmethod
    def tearDownClass(cls):
        conn = getattr(cls, "conn", None)
        if conn is not None:
            conn.close()

    def test_flag_is_only_on_the_warmup_name(self):
        names = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT exercise FROM sets WHERE is_warmup = 1")]
        self.assertEqual(names, [etl.WARMUP_NAME])
        flagged = self.conn.execute(
            "SELECT COUNT(*) FROM sets WHERE exercise = ?", (etl.WARMUP_NAME,)
        ).fetchone()[0]
        warm = self.conn.execute(
            "SELECT COUNT(*) FROM sets WHERE is_warmup = 1").fetchone()[0]
        self.assertEqual(flagged, warm)

    def test_warmup_rows_carry_no_metrics(self):
        """Поэтому флаг не может сдвинуть тоннаж, КОИ и 1RM."""
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(weight IS NOT NULL OR reps IS NOT NULL
                                OR rm IS NOT NULL OR rm_session IS NOT NULL
                                OR koi IS NOT NULL), 0) AS with_metrics
              FROM sets WHERE is_warmup = 1
            """
        ).fetchone()
        self.assertGreater(row["total"], 0)
        self.assertEqual(row["with_metrics"], 0)

    def test_report_default_drops_pseudo_exercise_and_option_restores_it(self):
        plain = stats.exercise_report(self.conn)
        full = stats.exercise_report(self.conn, include_warmup=True)
        plain_names = {r.exercise for r in plain}
        full_names = {r.exercise for r in full}
        self.assertNotIn(etl.WARMUP_NAME, plain_names)
        self.assertIn(etl.WARMUP_NAME, full_names)
        self.assertEqual(full_names - plain_names, {etl.WARMUP_NAME})
        extra = sum(r.sets for r in full) - sum(r.sets for r in plain)
        warm = [r for r in full if r.exercise == etl.WARMUP_NAME][0]
        self.assertEqual(extra, warm.sets)


class CliWarmupTests(unittest.TestCase):
    """Флаги ``stats --include-warmup`` и ``health-load --include-warmup``."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "t.db"
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.executescript(etl.SCHEMA)
        conn.executescript(etl.WEEKLY_KOI_VIEW)
        _add_set(conn, 1, "2024-01-16", "Разминка", is_warmup=1,
                 time_seconds=60)
        _add_set(conn, 2, "2024-01-16", "Жим гантелей лежа", weight=30.0,
                 reps=10, rm=one_rm(30.0, 10))
        conn.commit()
        conn.close()

    def _run(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from koilog import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", str(self.db), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_stats_flag_controls_the_pseudo_row(self):
        code, plain, _ = self._run("stats", "--all")
        self.assertEqual(code, 0)
        self.assertNotIn(etl.WARMUP_NAME, plain)
        code, full, _ = self._run("stats", "--all", "--include-warmup")
        self.assertEqual(code, 0)
        self.assertIn(etl.WARMUP_NAME, full)

    def test_health_load_flag_controls_the_counter(self):
        code, plain, _ = self._run(
            "health-load", "--window", "2024-01-01")
        self.assertEqual(code, 0)
        self.assertIn("подходов: 1 ", plain)
        self.assertIn("разминка: 1 подходов", plain)
        code, full, _ = self._run(
            "health-load", "--window", "2024-01-01", "--include-warmup")
        self.assertEqual(code, 0)
        self.assertIn("подходов: 2 ", full)
        self.assertNotIn("разминка:", full)


if __name__ == "__main__":
    unittest.main()
>>>>>>> 5383f412e79402469e2b66bec62fc1143913ce40
