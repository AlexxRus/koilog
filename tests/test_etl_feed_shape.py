"""W7-(d): проверки формы фида журнала.

Политика зафиксирована в `docs/feed-contract-spec.md` §7-бис (решение 4):
заголовок - жёстко (abort), монотонность/пустые/нечитаемые даты - warn,
потому что семантику загрузки они не меняют.

Отдельно проверяется **порядок**: форма и разбор идут до ``DELETE``. Прежний
код удалял источник и только потом разбирал файл, поэтому ошибка разбора
оставляла базу без источника.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from koilog import etl

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "tests" / "fixtures" / "feeds"

HEADER = (
    "ДАТА;ВРЕМЯ;ТРЕНИРОВКА;УПРАЖНЕНИЕ;ДИСТАНЦИЯ КМ;ВЕС;МНОЖИТЕЛЬ;"
    "ВРЕМЯ;ПОВТОРЫ;ОТДЫХ;ПРОПУЩЕН;ПРИМЕЧАНИЯ"
)


def write_tmp(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".csv", delete=False
    )
    with handle:
        handle.write(text)
    return Path(handle.name)


class _FeedCase(unittest.TestCase):
    def check(self, name: str) -> etl.FeedShape:
        return etl.check_feed(FEEDS / name)

    def assertAborts(self, path: Path, fragment: str) -> None:
        with self.assertRaises(etl.FeedShapeError) as caught:
            etl.check_feed(path)
        self.assertIn(fragment, str(caught.exception))
        # Совместимость: прежний контракт обещал ValueError на заголовке.
        self.assertIsInstance(caught.exception, ValueError)


class CleanFeedTests(_FeedCase):
    def test_clean_feed_has_no_warnings(self):
        shape = self.check("journal_ok.csv")
        self.assertEqual(shape.warnings, [])
        self.assertEqual(shape.records, 4)
        self.assertEqual(shape.data_records, 4)
        self.assertEqual(shape.blank_records, 0)
        self.assertEqual(shape.inversions, 0)
        self.assertEqual(shape.bad_dates, 0)
        self.assertEqual(shape.note_rows, 0)
        self.assertEqual(shape.header, etl.JOURNAL_COLUMNS)


class WarningTests(_FeedCase):
    def test_reversed_feed_warns_about_inversions(self):
        shape = self.check("journal_reversed.csv")
        self.assertEqual(shape.inversions, 1)
        self.assertTrue(any("не по возрастанию" in w for w in shape.warnings))

    def test_blank_density_warns(self):
        shape = self.check("journal_blanks.csv")
        self.assertEqual(shape.blank_records, 3)
        self.assertGreaterEqual(shape.blank_share, etl.BLANK_RECORD_WARN_SHARE)
        self.assertTrue(any("пустых записей" in w for w in shape.warnings))

    def test_note_rows_are_not_a_warning(self):
        """Строка-заметка (нет упражнения, эмодзи) - штатная форма канона."""
        shape = self.check("journal_notes.csv")
        self.assertEqual(shape.note_rows, 1)
        self.assertEqual(shape.warnings, [])

    def test_header_variant_warns_but_loads(self):
        shape = self.check("journal_header_variant.csv")
        self.assertEqual(len(shape.header), len(etl.JOURNAL_COLUMNS))
        self.assertTrue(any("имена колонок" in w for w in shape.warnings))
        self.assertEqual(shape.data_records, 2)

    def test_bad_date_in_the_middle_only_warns(self):
        path = write_tmp(
            HEADER + "\n"
            "01.01.20;10:00;Тренировка;Жим штанги;0;60;x;;5;90;;\n"
            ";10:00;Тренировка;Жим штанги;0;60;x;;5;90;;\n"
            "03.01.20;10:00;Тренировка;Жим штанги;0;60;x;;5;90;;\n"
        )
        try:
            shape = etl.check_feed(path)
        finally:
            path.unlink()
        self.assertEqual(shape.bad_dates, 1)
        self.assertEqual(shape.inversions, 0)
        self.assertTrue(any("нечитаемой датой" in w for w in shape.warnings))


class AbortTests(_FeedCase):
    def test_no_header_aborts(self):
        """Ярус i: первая строка - уже данные."""
        self.assertAborts(FEEDS / "journal_no_header.csv", "заголовок")

    def test_empty_file_aborts(self):
        path = write_tmp("")
        try:
            self.assertAborts(path, "пуст")
        finally:
            path.unlink()

    def test_wrong_column_count_aborts(self):
        """Ярус ii: число колонок не каноническое."""
        path = write_tmp("ДАТА;ВРЕМЯ;ТРЕНИРОВКА;УПРАЖНЕНИЕ\n01.01.20;10:00;Т;Ж\n")
        try:
            self.assertAborts(path, "колонок")
        finally:
            path.unlink()

    def test_bad_anchor_aborts(self):
        """Ярус iv: первая строка данных не даёт дату."""
        path = write_tmp(
            HEADER + "\n"
            "не-дата;10:00;Тренировка;Жим штанги;0;60;x;;5;90;;\n"
        )
        try:
            self.assertAborts(path, "не даёт дату")
        finally:
            path.unlink()


class OrderTests(unittest.TestCase):
    """Форма/разбор - до ``DELETE``: ошибка не должна обнулять источник."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = etl.connect(Path(self._dir.name) / "test.db")
        self.report = etl.load_all(
            self.conn,
            journal=FEEDS / "journal_ok.csv",
            report=None,
        )

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def _journal_rows(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT source_row, date, exercise, reps FROM sets "
            "WHERE source = ? ORDER BY source_row",
            (etl.SOURCE_JOURNAL,),
        ).fetchall()

    def test_shape_abort_leaves_the_source_untouched(self):
        before = self._journal_rows()
        self.assertEqual(len(before), self.report.journal)

        with self.assertRaises(etl.FeedShapeError):
            etl.load_all(self.conn, journal=FEEDS / "journal_no_header.csv",
                         report=None)

        self.assertEqual(self._journal_rows(), before)

    def test_warnings_reach_the_report(self):
        # Флаг: фид-реверс меньше прежнего, и это не суть теста - проверяются
        # предупреждения формы. Гард полноты проверяется в test_etl_feed_guard.
        report = etl.load_all(self.conn, journal=FEEDS / "journal_reversed.csv",
                              report=None, allow_incomplete_feed=True)
        self.assertTrue(any("не по возрастанию" in w for w in report.warnings))

    def test_clean_feed_reports_no_warnings(self):
        self.assertEqual(self.report.warnings, [])

    def test_a_different_feed_replaces_the_source(self):
        """Замена, а не дозапись: прежних строк источника не остаётся."""
        path = write_tmp(
            HEADER + "\n"
            "01.01.20;10:00;Тренировка;Жим гантелей;0;20;x;;8;90;;\n"
            "01.01.20;10:00;Тренировка;Жим гантелей;0;20;x;;8;90;;\n"
            "01.01.20;10:00;Тренировка;Тяга верхнего блока;0;50;x;;8;90;;\n"
            "03.01.20;10:00;Тренировка;Жим гантелей;0;20;x;;8;90;;\n"
        )
        try:
            report = etl.load_all(self.conn, journal=path, report=None)
        finally:
            path.unlink()
        self.assertEqual(report.journal, 4)
        exercises = {row[2] for row in self._journal_rows()}
        self.assertEqual(exercises, {"Жим гантелей", "Тяга верхнего блока"})


@unittest.skipUnless((ROOT / "data" / "journal.csv").exists(),
                     "нет data/journal.csv")
class CanonicalFeedTests(unittest.TestCase):
    """Канон не должен шуметь: все четыре класса аномалий молчат."""

    def test_canonical_journal_has_no_warnings(self):
        shape = etl.check_feed(etl.DEFAULT_JOURNAL)
        self.assertEqual(shape.warnings, [])
        self.assertEqual(shape.blank_records, 0)
        self.assertEqual(shape.inversions, 0)
        self.assertEqual(shape.bad_dates, 0)
        self.assertEqual(shape.header, etl.JOURNAL_COLUMNS)
        self.assertGreater(shape.data_records, 15_000)
        # Строки-заметки в каноне есть - и это не предупреждение.
        self.assertGreater(shape.note_rows, 0)


if __name__ == "__main__":
    unittest.main()
