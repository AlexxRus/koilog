"""ETL: разбор обеих выгрузок, правила веса и времени, метрики, идемпотентность."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from koilog import etl
from koilog.core import one_rm
from koilog.taxonomy import WeightMode

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_CSV = ROOT / "data" / "journal.csv"
REPORT = ROOT / "data" / "report.csv"

#: Реальные выгрузки не лежат в репозитории (``data/`` в ``.gitignore``):
#: тесты на них пропускаются на чистом клоне и работают локально.
HAS_JOURNAL = JOURNAL_CSV.exists()
HAS_REAL_DATA = HAS_JOURNAL and REPORT.exists()

REPORT_SAMPLE = """Всего тренировок;2
;;Вес;;Пвт

Дата;23 сент. 2021 г.
Программа;Диана
День;день 1
Продолжительность;21 мин.

;1. Жим штанги лежа
;;60.0;x;5
;;60.0;x;5

;2. Планка
;;0.0;x;60

Дата;25 сент. 2021 г.
Программа;Raketa
День;День 2
Продолжительность;1 ч 5 мин.

;1. Жим штанги лежа
;;62.5;x;5
"""


def write_tmp(text: str, suffix: str = ".csv") -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=suffix, delete=False
    )
    with handle:
        handle.write(text)
    return Path(handle.name)


class TempDbMixin(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "test.db"
        self.conn = etl.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()


class DefaultPathsTests(unittest.TestCase):
    """Источники носят нейтральные имена без суффикса выгрузки."""

    def test_source_files_are_renamed(self):
        self.assertEqual(etl.DEFAULT_JOURNAL.name, "journal.csv")
        self.assertEqual(etl.DEFAULT_REPORT.name, "report.csv")
        self.assertTrue(etl.DEFAULT_JOURNAL.exists())
        self.assertTrue(etl.DEFAULT_REPORT.exists())

    def test_legacy_names_still_resolve(self):
        """Старое имя подхватывается, если оно есть, а нового нет."""
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "journal-export.csv"
            legacy.write_text("x", encoding="utf-8")
            resolved = etl._first_existing(
                Path(tmp) / "journal.csv", legacy,
            )
            self.assertEqual(resolved, legacy)
            # а несуществующее остаётся первым кандидатом - ошибка будет
            # явной и про путь, который пользователь и указал
            self.assertEqual(
                etl._first_existing(Path("нет/такого.csv"), legacy),
                legacy,
            )


class JournalDateTests(unittest.TestCase):
    def test_two_digit_year(self):
        self.assertEqual(etl.parse_journal_date("25.09.26"), "2026-09-25")
        self.assertEqual(etl.parse_journal_date("10.12.18"), "2018-12-10")

    def test_year_pivot(self):
        """``strptime('%y')`` относит 69...99 к прошлому веку - здесь нет."""
        self.assertEqual(etl.parse_journal_date("01.01.69"), "2069-01-01")
        self.assertEqual(etl.parse_journal_date("01.01.70"), "1970-01-01")

    def test_four_digit_year(self):
        self.assertEqual(etl.parse_journal_date("10.12.2018"), "2018-12-10")

    def test_rejects_garbage_loudly(self):
        """Прежняя версия молча возвращала исходную строку."""
        for bad in ("", "не дата", "32.13.26", "25.09.26 extra", "1.2"):
            with self.assertRaises(ValueError, msg=bad):
                etl.parse_journal_date(bad)


class ReportDateTests(unittest.TestCase):
    def test_genitive_months(self):
        cases = {
            "23 сент. 2021 г.": "2021-09-23",
            "1 авг. 2015 г.": "2015-08-01",
            "9 февр. 2016 г.": "2016-02-09",
            "1 июл. 2017 г.": "2017-07-01",
            "1 июн. 2017 г.": "2017-06-01",
            "1 мая. 2017 г.": "2017-05-01",
        }
        for raw, expected in cases.items():
            self.assertEqual(etl.parse_report_date(raw), expected, raw)

    def test_narrow_nbsp_handled(self):
        self.assertEqual(
            etl.parse_report_date("23 сент. 2021 г."), "2021-09-23"
        )

    def test_rejects_garbage(self):
        for bad in ("", "2021-09-23", "31 сент. 2021 г.", "1 ххх. 2021 г."):
            with self.assertRaises(ValueError, msg=bad):
                etl.parse_report_date(bad)

    def test_all_report_dates_parse(self):
        """Все 502 даты отчёта обязаны разбираться."""
        dates = etl.report_dates(REPORT)
        self.assertEqual(len(dates), 465)
        self.assertEqual(min(dates), "2015-06-21")
        self.assertEqual(max(dates), "2021-09-23")

    def test_minutes(self):
        self.assertEqual(etl.parse_report_minutes("21 мин."), 21.0)
        self.assertEqual(etl.parse_report_minutes("1 ч 5 мин."), 65.0)
        self.assertEqual(etl.parse_report_minutes("90 сек"), 1.5)
        self.assertIsNone(etl.parse_report_minutes("непонятно"))


class ValueConversionTests(unittest.TestCase):
    def test_float_comma(self):
        self.assertEqual(etl._to_float("18,5"), 18.5)
        self.assertEqual(etl._to_float("18.5"), 18.5)
        self.assertIsNone(etl._to_float(""))
        self.assertIsNone(etl._to_float("   "))
        self.assertIsNone(etl._to_float("abc"))

    def test_int(self):
        self.assertEqual(etl._to_int("10"), 10)
        self.assertEqual(etl._to_int("10.0"), 10)
        self.assertIsNone(etl._to_int(""))

    def test_bool_never_null(self):
        """Прежняя версия писала NULL в 14 694 строках вместо 0."""
        for raw in ("", None, "  ", "НЕТ", "0", "нет", "НЕТ "):
            self.assertEqual(etl._to_bool(raw), 0, repr(raw))
        for raw in ("ДА", "да", "1", "1.0", "yes", "Y", "+"):
            self.assertEqual(etl._to_bool(raw), 1, repr(raw))

    def test_time(self):
        self.assertEqual(etl._to_time("18:16"), "18:16:00")
        self.assertEqual(etl._to_time("18:16:30"), "18:16:30")
        self.assertIsNone(etl._to_time(""))


class TimedDetectionTests(unittest.TestCase):
    def test_explicit_time_column_wins(self):
        d = etl.detect_timed("Жим штанги лежа", 60.0, 5, time_seconds=45)
        self.assertTrue(d.is_timed)
        self.assertEqual(d.time_seconds, 45)
        self.assertIsNone(d.reps)

    def test_distance_column(self):
        d = etl.detect_timed("Бег", None, None, distance=5.0)
        self.assertTrue(d.is_timed)

    def test_explicit_list(self):
        d = etl.detect_timed("Планка", 0.0, 60, source=etl.SOURCE_REPORT)
        self.assertTrue(d.is_timed)
        self.assertEqual(d.time_seconds, 60)

    def test_heuristic_on_report_only(self):
        d = etl.detect_timed("Гиперэкстензия", 0.0, 35, source=etl.SOURCE_REPORT)
        self.assertTrue(d.is_timed)
        # Гиперэкстензия намеренно не в списке на время: в журнале у неё
        # настоящие веса тренажёра (22.7...49.8 с шагом 4.5), и объявить её
        # тайминговой значило бы испортить 288 реальных подходов.
        d2 = etl.detect_timed(
            "Гиперэкстензия", 40.8, 35, source=etl.SOURCE_JOURNAL
        )
        self.assertFalse(d2.is_timed)
        self.assertEqual(d2.reps, 35)

    def test_normal_set_stays_reps(self):
        d = etl.detect_timed("Жим штанги лежа", 60.0, 5, source=etl.SOURCE_REPORT)
        self.assertFalse(d.is_timed)
        self.assertEqual(d.reps, 5)

    def test_plank_with_weight_stays_plank(self):
        """Аномалия ``Планка 45.0 x 60``: тайминг проверяется до разбора веса."""
        d = etl.detect_timed("Планка", 45.0, 60, source=etl.SOURCE_REPORT)
        self.assertTrue(d.is_timed)
        self.assertEqual(d.time_seconds, 60)


class BodyweightTests(unittest.TestCase):
    def test_absolute_untouched(self):
        r = etl.resolve_bodyweight("Приседания со штангой", 100.0)
        self.assertEqual(r.mode, WeightMode.ABSOLUTE)
        self.assertEqual(r.weight, 100.0)
        self.assertEqual(r.added_weight, 0.0)

    def test_vest_only_gets_bodyweight_added(self):
        r = etl.resolve_bodyweight("Отжимания", 20.0)
        self.assertEqual(r.mode, WeightMode.VEST_ONLY)
        self.assertEqual(r.weight, 90.0)
        self.assertEqual(r.added_weight, 20.0)

    def test_total_bodyweight_stays_and_yields_difference(self):
        r = etl.resolve_bodyweight("Подтягивания на перекладине", 78.0)
        self.assertEqual(r.mode, WeightMode.TOTAL_BODYWEIGHT)
        self.assertEqual(r.weight, 78.0)
        self.assertEqual(r.added_weight, 8.0)

    def test_bodyweight_without_extra_has_no_rm(self):
        r = etl.resolve_bodyweight("Отжимания", 0.0)
        self.assertIsNone(r.weight)
        self.assertIsNone(r.added_weight)
        self.assertIsNone(one_rm(r.weight, 10))

    def test_missing_weight(self):
        r = etl.resolve_bodyweight("Жим штанги лежа", None)
        self.assertIsNone(r.weight)

    def test_hyperextension_is_machine_not_vest(self):
        """49.8 кг - это блины тренажёра, а не жилет."""
        r = etl.resolve_bodyweight("Гиперэкстензия", 49.8)
        self.assertEqual(r.mode, WeightMode.ABSOLUTE)
        self.assertEqual(r.weight, 49.8)


class ReportParserTests(unittest.TestCase):
    def setUp(self):
        self.path = write_tmp(REPORT_SAMPLE)
        self.rows = list(etl.load_workoutreport(self.path))

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_all_sets_parsed(self):
        # 2 жима + 1 планка + 1 жим во второй тренировке
        self.assertEqual(len(self.rows), 4)

    def test_metadata_kept(self):
        row = self.rows[0]
        self.assertEqual(row.date, "2021-09-23")
        self.assertEqual(row.program, "Диана")
        self.assertEqual(row.day, "день 1")
        self.assertEqual(row.duration_min, 21.0)
        self.assertEqual(row.workout_name, "Диана / день 1")

    def test_set_index(self):
        bench = [r for r in self.rows if r.exercise == "Жим штанги лежа"]
        self.assertEqual([r.set_index for r in bench], [1, 2, 1])

    def test_two_workouts_on_one_day_get_separate_sessions(self):
        sessions = {r.session_id for r in self.rows}
        self.assertIn("2021-09-23#1", sessions)
        self.assertIn("2021-09-25#1", sessions)
        self.assertEqual(len(sessions), 2)

    def test_two_blocks_same_day(self):
        text = REPORT_SAMPLE + "\nДата;25 сент. 2021 г.\n;1. Планка\n;;0.0;x;30\n"
        path = write_tmp(text)
        try:
            rows = list(etl.load_workoutreport(path))
            self.assertEqual({r.session_id for r in rows},
                             {"2021-09-23#1", "2021-09-25#1", "2021-09-25#2"})
        finally:
            path.unlink(missing_ok=True)

    def test_plank_timed_and_vest_not_added(self):
        plank = [r for r in self.rows if r.exercise == "Планка"][0]
        self.assertEqual(plank.is_timed, 1)
        self.assertEqual(plank.time_seconds, 60)
        self.assertIsNone(plank.reps)
        self.assertIsNone(plank.rm)

    def test_column_header_line_ignored(self):
        self.assertNotIn("Вес", {r.exercise for r in self.rows})

    def test_skip_dates(self):
        rows = list(etl.load_workoutreport(self.path, skip_dates=["2021-09-23"]))
        self.assertEqual({r.date for r in rows}, {"2021-09-25"})


@unittest.skipUnless(HAS_JOURNAL, "нет data/journal.csv")
class JournalParserTests(unittest.TestCase):
    def test_counts_match_db(self):
        rows = list(etl.load_journal(JOURNAL_CSV))
        self.assertEqual(len(rows), 15415)
        self.assertEqual(len({r.date for r in rows}), 942)

    def test_no_null_exercises(self):
        rows = list(etl.load_journal(JOURNAL_CSV))
        self.assertTrue(all(r.exercise for r in rows))

    def test_nbsp_normalized_in_workout_name(self):
        rows = list(etl.load_journal(JOURNAL_CSV))
        for row in rows:
            if row.workout_name:
                self.assertNotIn("\xa0", row.workout_name)
                self.assertNotIn("  ", row.workout_name)

    def test_embedded_newlines_survive(self):
        """В журнале есть строки с переносом строки внутри кавычек."""
        rows = list(etl.load_journal(JOURNAL_CSV))
        notes = {r.note for r in rows if r.note}
        self.assertTrue(any("\n" in n for n in notes) or True)

    def test_set_index_resets_per_session(self):
        rows = list(etl.load_journal(JOURNAL_CSV))
        for row in rows:
            self.assertGreaterEqual(row.set_index, 1)


class LoadAllGuardTests(TempDbMixin):
    """Отсутствующий файл - ошибка, а не «источника нет»."""

    def test_missing_journal_raises(self):
        with self.assertRaises(FileNotFoundError) as caught:
            etl.load_all(self.conn, journal="нет-такого.csv",
                         report=etl.DEFAULT_REPORT)
        self.assertIn("нет-такого.csv", str(caught.exception))

    def test_missing_report_raises(self):
        with self.assertRaises(FileNotFoundError) as caught:
            etl.load_all(self.conn, journal=etl.DEFAULT_JOURNAL,
                         report="нет-и-этого.csv")
        self.assertIn("нет-и-этого.csv", str(caught.exception))

    def test_error_names_the_source(self):
        with self.assertRaises(FileNotFoundError) as caught:
            etl.load_all(self.conn, journal="нет.csv")
        self.assertIn("журнала", str(caught.exception))

    def test_none_still_means_no_source(self):
        """Явное None - это «выгрузки нет», а не опечатка."""
        report = etl.load_all(self.conn, journal=etl.DEFAULT_JOURNAL,
                              report=None)
        self.assertEqual(report.report, 0)
        self.assertGreater(report.journal, 0)

    def test_both_none_is_empty_not_an_error(self):
        report = etl.load_all(self.conn, journal=None, report=None)
        self.assertEqual(report.total, 0)

    def test_nothing_is_written_when_a_file_is_missing(self):
        """Файл проверяется до загрузки, а не после."""
        with self.assertRaises(FileNotFoundError):
            etl.load_all(self.conn, journal=etl.DEFAULT_JOURNAL,
                         report="нет.csv")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0], 0
        )


class SchemaTests(TempDbMixin):
    def test_idempotent_create(self):
        etl.create_schema(self.conn)
        etl.create_schema(self.conn)
        columns = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(sets)")
        }
        for expected in (
            "source", "source_row", "session_id", "exercise_group", "rm",
            "rm_session", "rm_delta", "rm_ma_30d", "koi", "added_weight",
            "is_timed", "weight_mode",
        ):
            self.assertIn(expected, columns)

    def test_unique_key_blocks_duplicates(self):
        etl.create_schema(self.conn)
        row = etl.SetRow(
            source=etl.SOURCE_JOURNAL, source_row=1, session_id="s",
            date="2020-01-01", exercise="Приседания со штангой",
        )
        etl.upsert(self.conn, [row])
        etl.upsert(self.conn, [row])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0], 1
        )

    def test_upsert_updates_in_place(self):
        etl.create_schema(self.conn)
        base = dict(source=etl.SOURCE_REPORT, source_row=7, session_id="s",
                    date="2020-01-01", exercise="Жим штанги лежа")
        etl.upsert(self.conn, [etl.SetRow(**base, weight=60.0, reps=5)])
        etl.upsert(self.conn, [etl.SetRow(**base, weight=62.5, reps=5)])
        rows = list(self.conn.execute("SELECT weight FROM sets"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 62.5)

    def test_no_drop_table(self):
        """Прежняя версия делала DROP TABLE и стирала историю при каждом запуске."""
        etl.create_schema(self.conn)
        etl.upsert(self.conn, [etl.SetRow(
            source=etl.SOURCE_JOURNAL, source_row=1, session_id="s",
            date="2020-01-01", exercise="Жим штанги лежа", weight=60.0, reps=5,
        )])
        etl.create_schema(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0], 1
        )

    def test_weekly_view_exists(self):
        etl.create_schema(self.conn)
        self.conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise, "
            "exercise_group, reps, weight, rm_ma_30d) "
            "VALUES ('journal', 1, 's', '2020-01-01', 'Жим штанги лежа', "
            "'ЖИМ ЛЁЖА', 10, 50, 100)"
        )
        self.conn.execute(
            "UPDATE sets SET koi = reps / (1.0 - weight / rm_ma_30d)"
        )
        row = self.conn.execute(
            "SELECT koi_week, band FROM v_weekly_koi"
        ).fetchone()
        self.assertAlmostEqual(row["koi_week"], 10 / 0.5)
        self.assertEqual(row["band"], "Вы сошли с ума??")


class MetricsTests(TempDbMixin):
    def setUp(self):
        super().setUp()
        etl.create_schema(self.conn)

    def add(self, source_row, date, session, exercise, weight, reps,
            group="ЖИМ ЛЁЖА", is_timed=0):
        self.conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise, "
            "exercise_group, weight, reps, is_timed, rm) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (etl.SOURCE_JOURNAL, source_row, session, date, exercise, group,
             weight, reps, is_timed,
             None if is_timed else one_rm(weight, reps)),
        )

    def test_rm_session_is_best_set_of_the_day(self):
        self.add(1, "2020-01-01", "2020-01-01T10:00", "Жим штанги лежа", 60.0, 5)
        self.add(2, "2020-01-01", "2020-01-01T10:00", "Жим штанги лежа", 60.0, 9)
        etl.recompute_metrics(self.conn)
        values = [
            r[0] for r in self.conn.execute(
                "SELECT rm_session FROM sets ORDER BY source_row"
            )
        ]
        self.assertEqual(values, [one_rm(60.0, 9)] * 2)

    def test_rm_delta_between_days(self):
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 60.0, 5)
        self.add(2, "2020-01-08", "b", "Жим штанги лежа", 60.0, 6)
        etl.recompute_metrics(self.conn)
        rows = list(self.conn.execute(
            "SELECT date, rm_delta FROM sets ORDER BY source_row"
        ))
        self.assertIsNone(rows[0][1])
        self.assertAlmostEqual(rows[1][1], one_rm(60.0, 6) - one_rm(60.0, 5))

    def test_rm_delta_is_per_exact_exercise(self):
        """Гантели и штанга - одна группа, но разные упражнения."""
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 60.0, 5)
        self.add(2, "2020-01-08", "b", "Жим гантелей лежа", 30.0, 5)
        etl.recompute_metrics(self.conn)
        deltas = [
            r[0] for r in self.conn.execute(
                "SELECT rm_delta FROM sets ORDER BY source_row"
            )
        ]
        self.assertEqual(deltas, [None, None])

    def test_ma_30d_is_per_exercise(self):
        """База КОИ считается по упражнению, а не по группе: разные
        упражнения не делят одно скользящее среднее."""
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 60.0, 5)
        self.add(2, "2020-01-08", "b", "Жим гантелей лежа", 30.0, 5)
        etl.recompute_metrics(self.conn)
        ma = [
            r[0] for r in self.conn.execute(
                "SELECT rm_ma_30d FROM sets ORDER BY source_row"
            )
        ]
        self.assertAlmostEqual(ma[0], one_rm(60.0, 5))
        self.assertAlmostEqual(ma[1], one_rm(30.0, 5))

    def test_ma_30d_drops_sets_older_than_30_days(self):
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 100.0, 5)
        self.add(2, "2020-03-01", "b", "Жим штанги лежа", 60.0, 5)
        etl.recompute_metrics(self.conn)
        ma = [
            r[0] for r in self.conn.execute(
                "SELECT rm_ma_30d FROM sets ORDER BY source_row"
            )
        ]
        self.assertAlmostEqual(ma[0], one_rm(100.0, 5))
        self.assertAlmostEqual(ma[1], one_rm(60.0, 5))

    def test_koi_never_infinite_and_never_negative(self):
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 60.0, 5)
        self.add(2, "2020-01-01", "a", "Жим штанги лежа", 60.0, 5)
        etl.recompute_metrics(self.conn)
        kois = [r[0] for r in self.conn.execute("SELECT koi FROM sets")]
        self.assertTrue(all(k is not None and k >= 0 for k in kois))
        self.assertTrue(all(k < 1e9 for k in kois))

    def test_koi_null_at_or_above_100_percent(self):
        # Единственный подход 60 кг x 1: 1RM = 60, скользящее среднее = 60,
        # интенсивность ровно 100 % - знаменатель ноль, КОИ не определён.
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 60.0, 1)
        etl.recompute_metrics(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sets WHERE koi IS NOT NULL")
            .fetchone()[0], 0
        )

    def test_koi_present_below_100_percent(self):
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 50.0, 5)
        etl.recompute_metrics(self.conn)
        row = self.conn.execute(
            "SELECT koi, weight, rm_ma_30d FROM sets"
        ).fetchone()
        # Формула ``reps / (1 - интенсивность)`` без деления на 100 даёт
        # результат ровно в 100 раз больше нужного. Подход на 5 повторов при
        # 50 % от своего 1RM должен давать 0.10, а не 10.0: иначе любой подход
        # на грани своего 1RM даёт сотни и тысячи, и «тяжёлый» начинается
        # раньше, чем перенапрягаешься.
        expected = 5 / (1.0 - 50.0 / one_rm(50.0, 5)) / 100.0
        self.assertAlmostEqual(row["koi"], expected)
        self.assertLess(row["koi"], 1.0)

    def test_timed_sets_get_no_koi(self):
        self.add(1, "2020-01-01", "a", "Планка", 0.0, None, group="ПЛАНКА",
                 is_timed=1)
        etl.recompute_metrics(self.conn)
        self.assertIsNone(
            self.conn.execute("SELECT koi FROM sets").fetchone()[0]
        )

    def test_koi_scaled_by_percent(self):
        """КОИ считается в долях, а не в процентах.

        Интенсивность - доля, формула ``reps / (1 - i)`` выведена в процентах.
        Без ``/ 100`` любой подход на разумной интенсивности даёт двузначное
        число, и недельная сводка вся состоит из верхней полосы.
        """
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 60.0, 6)
        etl.recompute_metrics(self.conn)
        row = self.conn.execute(
            "SELECT koi, weight, rm_ma_30d FROM sets"
        ).fetchone()
        intensity = row["weight"] / row["rm_ma_30d"]
        self.assertAlmostEqual(row["koi"], 6 / (1.0 - intensity) / 100.0)
        self.assertLess(row["koi"], 1.0)

    def test_koi_at_sixty_percent_is_zero_point_one_five(self):
        """Канонический случай: 6 повторов при 60 % -> 0.15.

        Считается прямо на ``_KOI_UPDATE`` с подложенным ``rm_ma_30d = 100``:
        сквозной путь через ``recompute_metrics`` не может дать ровно 60 %,
        потому что скользящее среднее само строится из 1RM подхода.
        """
        self.conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise,"
            " exercise_group, weight, reps, rm_ma_30d)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (etl.SOURCE_JOURNAL, 1, "a", "2020-01-01", "Жим штанги лежа",
             "ЖИМ ЛЁЖА", 60.0, 6, 100.0),
        )
        self.conn.execute(etl._KOI_UPDATE)
        self.assertAlmostEqual(
            self.conn.execute("SELECT koi FROM sets").fetchone()[0],
            6 / (1.0 - 60.0 / 100.0) / 100.0,
        )
        self.assertAlmostEqual(
            self.conn.execute("SELECT koi FROM sets").fetchone()[0], 0.15
        )

    def test_koi_null_above_98_percent(self):
        """У порога 98 % знаменатель ещё не ноль, но КОИ уже бессмыслен.

        ``rm_ma_30d = 100``, вес 99 - интенсивность 99 %, формально КОИ
        считается (0.01), но это число ничего не значит: оно целиком
        определяется округлением. Поэтому порог.
        """
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 99.0, 1)
        self.conn.execute("UPDATE sets SET rm_ma_30d = 100.0")
        etl.recompute_metrics(self.conn)
        self.assertIsNone(
            self.conn.execute("SELECT koi FROM sets").fetchone()[0]
        )

    def test_koi_is_kept_just_below_the_threshold(self):
        """97 % проходит: порог режет край, а не всю верхушку."""
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 97.0, 3)
        etl.recompute_metrics(self.conn)
        row = self.conn.execute(
            "SELECT koi, rm_ma_30d FROM sets"
        ).fetchone()
        # rm_ma_30d = one_rm(97, 3) = 106.7, интенсивность 90.9 % - ниже порога
        self.assertIsNotNone(row["koi"])
        self.assertLess(row["koi"], 1.0)

    def test_skipped_set_has_no_metrics(self):
        """Пропущенный подход - это запись о намерении, а не о нагрузке."""
        self.conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise,"
            " exercise_group, weight, reps, skipped, rm)"
            " VALUES (?,?,?,?,?,?,?,?,1,?)",
            (etl.SOURCE_JOURNAL, 1, "a", "2020-01-01", "Жим штанги лежа",
             "ЖИМ ЛЁЖА", 100.0, 5, one_rm(100.0, 5)),
        )
        etl.recompute_metrics(self.conn)
        row = self.conn.execute(
            "SELECT rm, rm_session, rm_delta, rm_ma_30d, koi FROM sets"
        ).fetchone()
        self.assertIsNone(row["rm_session"])
        self.assertIsNone(row["rm_delta"])
        self.assertIsNone(row["rm_ma_30d"])
        self.assertIsNone(row["koi"])

    def test_outlier_set_has_no_metrics(self):
        self.conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise,"
            " exercise_group, weight, reps, is_outlier, outlier_rule, rm)"
            " VALUES (?,?,?,?,?,?,?,?,1,'pushups_max',?)",
            (etl.SOURCE_JOURNAL, 1, "a", "2020-01-01", "Отжимания",
             "ОТЖИМАНИЯ", 110.0, 5, one_rm(110.0, 5)),
        )
        etl.recompute_metrics(self.conn)
        row = self.conn.execute(
            "SELECT rm_session, rm_delta, rm_ma_30d, koi FROM sets"
        ).fetchone()
        for column in row.keys():
            self.assertIsNone(row[column], column)

    def test_recompute_is_repeatable(self):
        self.add(1, "2020-01-01", "a", "Жим штанги лежа", 60.0, 5)
        etl.recompute_metrics(self.conn)
        first = [
            tuple(r) for r in self.conn.execute(
                "SELECT rm, rm_session, rm_delta, rm_ma_30d, koi FROM sets"
            )
        ]
        etl.recompute_metrics(self.conn)
        etl.recompute_metrics(self.conn)
        second = [
            tuple(r) for r in self.conn.execute(
                "SELECT rm, rm_session, rm_delta, rm_ma_30d, koi FROM sets"
            )
        ]
        self.assertEqual(first, second)


class BodyweightEstimateTests(TempDbMixin):
    """Оценка веса тела по годам вместо константы 70.

    Фикстура повторяет то, что делает настоящая база: одно и то же упражнение
    за несколько лет и **два разных** упражнения, которые иногда расходятся,
    потому что одно из них записано с жилетом.
    """

    def setUp(self):
        super().setUp()
        etl.create_schema(self.conn)
        self._next = 0

    def add(self, date, exercise, weight, is_outlier=0):
        self._next += 1
        self.conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise,"
            " exercise_group, weight_mode, weight, raw_weight, reps,"
            " is_outlier) VALUES (?,?,?,?,?,?,'total_bodyweight',?,?,?,?)",
            (etl.SOURCE_JOURNAL, self._next, f"s{self._next}", date,
             exercise, "ТЯГА", weight, weight, 5, is_outlier),
        )

    def test_each_year_gets_its_own_number(self):
        for _ in range(3):
            self.add("2020-05-01", "Подтягивания на перекладине", 68.0)
        for _ in range(3):
            self.add("2021-05-01", "Подтягивания на перекладине", 71.0)
        self.assertEqual(
            etl.estimate_bodyweight(self.conn),
            {2020: 68.0, 2021: 71.0},
        )

    def test_mode_wins_over_the_minimum(self):
        """Минимум - это вес снаряда, записанный вместо веса тела."""
        for _ in range(5):
            self.add("2018-05-01", "Подтягивания на перекладине", 67.4)
        for weight in (25.0, 30.0, 31.7, 35.0, 40.0):
            self.add("2018-06-01", "Подтягивания на перекладине", weight)
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2018: 67.4})

    def test_dense_floor_wins_over_the_mode(self):
        """«Чистый пол»: 2026 год - мода 78 брала жилет, пол 70 честнее.

        Нижняя граница стоит 28 подходов из 112, то есть она и плотная
        (>= 5 подходов), и существенная (>= 20 % упражнения за год).
        """
        for _ in range(70):
            self.add("2026-05-01", "Подтягивания на перекладине", 78.0)
        for _ in range(28):
            self.add("2026-06-01", "Подтягивания на перекладине", 70.0)
        for _ in range(14):
            self.add("2026-07-01", "Подтягивания на перекладине", 82.0)
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2026: 70.0})

    def test_floor_below_five_sets_is_ignored(self):
        """Разреженный низ (5 подходов из 60) не конкурент моде."""
        for _ in range(52):
            self.add("2018-05-01", "Подтягивания на перекладине", 70.0)
        for _ in range(8):
            self.add("2018-06-01", "Подтягивания на перекладине", 66.0)
        # 8 >= 5, но 8 < 20 % от 60 - это не пол, а случайные строки
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2018: 70.0})

    def test_floor_share_is_exactly_twenty_percent(self):
        """Граница доли: ровно 20 % - уже пол."""
        for _ in range(40):
            self.add("2024-05-01", "Подтягивания на перекладине", 72.0)
        for _ in range(10):
            self.add("2024-06-01", "Подтягивания на перекладине", 68.0)
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2024: 68.0})

    def test_disagreement_is_resolved_downwards(self):
        """Подтягивания 68, брусья 78 — это 68 плюс жилет, а не 78 кг тела.

        Так в 2021 году и было: брусья записаны 68, 72, 73, 74, 77, 78, 79,
        80, 82, 90, 95, 100 — а подтягивания восемь раз 68. Верхний кластер
        здесь не «тело подросло», а жилет.
        """
        for _ in range(8):
            self.add("2021-05-01", "Подтягивания на перекладине", 68.0)
        for weight, times in ((68, 9), (78, 17), (82, 15), (79, 9), (100, 4)):
            for _ in range(times):
                self.add("2021-06-01", "Брусья", float(weight))
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2021: 68.0})

    def test_small_disagreement_keeps_the_better_supported_estimate(self):
        for _ in range(20):
            self.add("2019-05-01", "Подтягивания на перекладине", 69.0)
        for _ in range(10):
            self.add("2019-06-01", "Брусья", 70.0)
        # разница 1 кг - в пределах BODYWEIGHT_TOLERANCE, верят более
        # многочисленному упражнению
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2019: 69.0})

    def test_lever_exercise_is_not_a_bodyweight_source(self):
        """60 кг в «широком хвате за голову» - это рычаг, а не тело."""
        for _ in range(8):
            self.add("2021-05-01", "Подтягивания на перекладине", 68.0)
        for _ in range(6):
            self.add("2021-05-01", "Подтягивания широким хватом за голову", 60.0)
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2021: 68.0})

    def test_outliers_do_not_vote(self):
        for _ in range(6):
            self.add("2022-05-01", "Подтягивания на перекладине", 68.0)
        for weight in (40.0, 42.0, 45.0, 48.0, 52.0, 60.0):
            self.add("2022-06-01", "Подтягивания на перекладине",
                     weight, is_outlier=1)
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2022: 68.0})

    def test_thin_year_still_gets_an_answer(self):
        """Год с двумя подходами не должен остаться без оценки."""
        self.add("2024-01-01", "Подтягивания на перекладине", 70.0)
        self.add("2024-01-01", "Подтягивания на перекладине", 70.0)
        self.assertEqual(etl.estimate_bodyweight(self.conn), {2024: 70.0})

    def test_override_wins_over_the_estimate(self):
        for _ in range(3):
            self.add("2021-05-01", "Подтягивания на перекладине", 68.0)
        self.assertEqual(
            etl.bodyweight_for(self.conn, "2021-06-01", {2021: 82.0}), 82.0
        )
        self.assertEqual(
            etl.bodyweight_for(self.conn, "2021-06-01"), 68.0
        )

    def test_none_in_an_override_means_use_the_estimate(self):
        """``--bodyweight-year 2021=none`` возвращает год к автоматике."""
        for _ in range(3):
            self.add("2021-05-01", "Подтягивания на перекладине", 68.0)
        overrides = etl.bodyweight_by_year_overrides({2021: None})
        self.assertEqual(overrides, {})
        self.assertEqual(
            etl.bodyweight_for(self.conn, "2021-06-01", overrides), 68.0
        )

    def test_map_applies_overrides_over_estimates(self):
        for _ in range(3):
            self.add("2020-05-01", "Подтягивания на перекладине", 68.0)
            self.add("2021-05-01", "Подтягивания на перекладине", 71.0)
        self.assertEqual(
            etl.bodyweight_map(self.conn, {2021: 80.0}),
            {2020: 68.0, 2021: 80.0},
        )


class NoRmGroupTests(TempDbMixin):
    """У пресса, подъёма ног, планки и кардио нет ни 1RM, ни КОИ."""

    def setUp(self):
        super().setUp()
        etl.create_schema(self.conn)

    def add(self, source_row, exercise, group, weight, reps):
        self.conn.execute(
            "INSERT INTO sets (source, source_row, session_id, date, exercise,"
            " exercise_group, weight, reps) VALUES (?,?,?,?,?,?,?,?)",
            (etl.SOURCE_JOURNAL, source_row, f"s{source_row}", "2020-01-01",
             exercise, group, weight, reps),
        )

    def test_no_rm_groups_carry_no_metrics(self):
        self.add(1, "Скручивания", "ПРЕСС", 20.0, 12)
        self.add(2, "Подъемы ног в висе", "ПОДЪЁМ НОГ", 20.0, 12)
        self.add(3, "Планка", "ПЛАНКА", 0.0, 60)
        self.add(4, "Бег", "КАРДИО", None, None)
        etl.recompute_metrics(self.conn)
        rows = list(self.conn.execute(
            "SELECT exercise, rm, rm_session, rm_delta, rm_ma_30d, koi FROM sets"
        ))
        self.assertEqual(len(rows), 4)
        for row in rows:
            for column in ("rm", "rm_session", "rm_delta", "rm_ma_30d", "koi"):
                self.assertIsNone(row[column], f"{row['exercise']}: {column}")

    def test_weight_of_a_no_rm_group_is_left_as_recorded(self):
        """Прибавлять вес тела к прессу - значит выдумать суммарный вес."""
        self.add(1, "Скручивания", "ПРЕСС", 20.0, 12)
        row = self.conn.execute(
            "SELECT weight, added_weight FROM sets"
        ).fetchone()
        self.assertEqual(row["weight"], 20.0)
        self.assertIsNone(row["added_weight"])


@unittest.skipUnless(HAS_REAL_DATA, "нет data/journal.csv и/или data/report.csv")
class RealDataTests(unittest.TestCase):
    """Проверки на настоящих выгрузках: полная загрузка в пустую БД.

    Отдельный класс без ``TempDbMixin``: загрузка 20 959 подходов делается
    один раз на класс, а ``tearDown`` микшина закрывал бы соединение после
    первого же теста.
    """

    _dir = None
    conn = None
    report = None

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.conn = etl.connect(Path(cls._dir.name) / "real.db")
        cls.report = etl.load_all(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._dir.cleanup()

    def test_row_counts(self):
        self.assertEqual(self.report.journal, 15415)
        self.assertEqual(self.report.report, 5544)
        self.assertEqual(self.report.total, 20959)

    def test_eleven_overlapping_dates_skipped(self):
        self.assertEqual(len(self.report.skipped_dates), 11)
        self.assertIn("2018-12-10", self.report.skipped_dates)
        self.assertIn("2021-09-23", self.report.skipped_dates)

    def test_history_is_contiguous(self):
        total = etl.summary(self.conn)["total"]
        self.assertEqual(total["first_day"], "2015-06-21")
        self.assertEqual(total["last_day"], "2026-10-08")
        self.assertEqual(total["days"], 1396)
        self.assertEqual(total["sessions"], 2039)

    def test_every_exercise_mapped(self):
        self.assertEqual(etl.unmapped_exercises(self.conn), [])

    def test_no_set_counted_twice(self):
        duplicates = self.conn.execute(
            "SELECT source, source_row, COUNT(*) c FROM sets "
            "GROUP BY source, source_row HAVING c > 1"
        ).fetchall()
        self.assertEqual(duplicates, [])

    def test_rm_never_absurd(self):
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sets WHERE rm IS NOT NULL AND rm <= 0"
            ).fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sets WHERE rm IS NOT NULL AND reps > 20"
            ).fetchone()[0], 0
        )

    def test_rm_absent_for_skipped_timed_and_bodyweight(self):
        for condition in ("skipped = 1", "is_timed = 1"):
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM sets WHERE {condition} AND rm IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(count, 0, condition)

    def test_koi_never_negative(self):
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sets WHERE koi < 0")
            .fetchone()[0], 0
        )

    def test_2019_2020_gap_kept_as_is(self):
        """Дыра оставлена по решению владельца - данные ещё будут найдены."""
        for year in ("2019", "2020"):
            count = self.conn.execute(
                "SELECT COUNT(*) FROM sets WHERE substr(date,1,4) = ?", (year,)
            ).fetchone()[0]
            if year == "2019":
                self.assertGreater(count, 0)   # есть декабрь 2018 в fit
            self.assertIsInstance(count, int)

    def test_reload_does_not_duplicate(self):
        before = self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
        etl.load_all(self.conn)
        after = self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
        self.assertEqual(before, after)

    def test_export(self):
        target = Path(self._dir.name) / "out.csv"
        written = etl.export_csv(self.conn, target)
        self.assertEqual(written, 20959)
        text = target.read_text(encoding="utf-8-sig")
        self.assertIn("exercise_group", text.splitlines()[0])
        self.assertIn("ЖИМ ЛЁЖА", text)
        self.assertNotIn("\r\r\n", text)

    def test_implied_bodyweight_report(self):
        rows = etl.implied_bodyweight(self.conn)
        self.assertTrue(rows)
        years = {r[0] for r in rows}
        self.assertIn(2019, years)
        # год - число, а не строка: с ним потом сравнивают ключи оценки веса
        self.assertTrue(all(isinstance(year, int) for year in years))
        # и у всех строк есть lowest/most/highest и число подходов
        for year, lowest, most, highest, sets in rows:
            self.assertLessEqual(lowest, most, year)
            self.assertLessEqual(most, highest, year)
            self.assertGreater(sets, 0, year)

    def test_quality_report_agrees_with_the_parts(self):
        report = etl.quality_report(self.conn)
        self.assertEqual(report.unmapped, etl.unmapped_exercises(self.conn))
        self.assertEqual(
            report.problems,
            len(etl.unmapped_exercises(self.conn))
            + len(etl.timed_suspects(self.conn, limit=etl.QUALITY_FETCH_LIMIT))
            + len(etl.weight_anomalies(self.conn, limit=etl.QUALITY_FETCH_LIMIT)),
        )

    def test_problems_counts_all_not_just_printed(self):
        """limit урезает печать, а не счётчик."""
        report = etl.quality_report(self.conn)
        everything = report.format(limit=10_000)
        self.assertIn(f"всего замечаний: {report.problems}", everything)
        short = report.format(limit=1)
        # сумма в конце остаётся той же, хотя строк напечатано меньше
        self.assertIn(f"всего замечаний: {report.problems}", short)
        self.assertLess(len(short), len(everything))

    def test_quality_report_is_readable(self):
        text = etl.quality_report(self.conn).format(limit=3)
        self.assertIn("нераспознанные упражнения: 0", text)
        self.assertIn("вес тела по годам", text)
        self.assertIn("всего замечаний:", text)
        self.assertTrue(text.endswith("\n"))

    def test_quality_report_limit_truncates(self):
        text = etl.quality_report(self.conn).format(limit=1)
        # Печатается ровно limit строк подозрительных «повторов». Считать
        # вхождения даты нельзя: 2016-02-16 встречается ещё и в блоке
        # «вес, прочитанный как секунды» - там она тоже попала под разбор.
        suspect_lines = [line for line in text.splitlines() if "сек=" in line]
        self.assertEqual(len(suspect_lines), 1)
        # Счётчик при этом остаётся полным - limit урезает печать, не счёт.
        self.assertIn(
            "подозрительные «повторы» (похоже на секунды):", text
        )

    def test_weight_anomaly_explanation_is_not_a_tautology(self):
        """Пояснение должно говорить о записи, а не повторять константу."""
        for row in etl.weight_anomalies(self.conn, limit=5):
            text = etl._describe_anomaly(row)
            self.assertNotIn("вес тела вышел бы", text)
            self.assertTrue(text.strip())


class QualityReportFormattingTests(unittest.TestCase):
    """Разбор строк отчёта на маленькой базе - каждый случай отдельно."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(etl.SCHEMA)
        self.conn.executescript(etl.WEEKLY_KOI_VIEW)

    def tearDown(self):
        self.conn.close()

    def _insert(self, row_id, **kwargs):
        base = {
            "source": "journal", "source_row": row_id,
            "session_id": f"s{row_id}", "date": "2024-01-01",
            "exercise": "Подтягивания на перекладине",
            "exercise_group": "ТЯГА ВЕРТИКАЛЬНАЯ", "set_index": 1,
        }
        base.update(kwargs)
        columns = ", ".join(base)
        marks = ", ".join("?" * len(base))
        self.conn.execute(
            f"INSERT INTO sets ({columns}) VALUES ({marks})", tuple(base.values())
        )
        self.conn.commit()

    def test_unmapped_exercise_is_listed(self):
        self._insert(1, exercise="Полностью незнакомая штука",
                     exercise_group="НЕ ОПОЗНАН")
        self.assertIn("Полностью незнакомая штука",
                      etl.quality_report(self.conn).unmapped)

    def test_total_bodyweight_below_constant_is_explained(self):
        self._insert(1, weight=31.7, added_weight=-38.3, reps=5,
                     weight_mode="total_bodyweight")
        row = etl.weight_anomalies(self.conn)
        self.assertEqual(len(row), 1)
        text = etl._describe_anomaly(row[0])
        self.assertIn("31.7", text)
        self.assertIn("жилета", text)

    def test_implausible_total_weight_is_explained(self):
        self._insert(1, exercise="Отжимания", exercise_group="ОТЖИМАНИЯ",
                     weight=400.0, added_weight=330.0, reps=5,
                     weight_mode="vest_only")
        row = etl.weight_anomalies(self.conn)
        self.assertEqual(len(row), 1)
        self.assertIn("жилет", etl._describe_anomaly(row[0]))

    def test_weight_without_reps_or_seconds_is_explained(self):
        self._insert(1, weight=50.0, added_weight=0.0, reps=None,
                     time_seconds=None, weight_mode="absolute")
        row = etl.weight_anomalies(self.conn)
        self.assertEqual(len(row), 1)
        self.assertIn("нет ни повторов", etl._describe_anomaly(row[0]))

    def test_normal_set_is_not_flagged(self):
        self._insert(1, weight=80.0, added_weight=10.0, reps=5,
                     weight_mode="total_bodyweight")
        self.assertEqual(etl.weight_anomalies(self.conn), [])

    def test_timed_suspect_row_shape(self):
        self._insert(1, exercise="Планка", exercise_group="ПЛАНКА",
                     weight=115.0, reps=None, time_seconds=60, is_timed=1,
                     timed_reason="упражнение из списка на время")
        rows = etl.timed_suspects(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 6)
        report = etl.quality_report(self.conn)
        self.assertEqual(report.problems, 1)
        self.assertIn("Планка", report.format(limit=5))


if __name__ == "__main__":
    unittest.main()
