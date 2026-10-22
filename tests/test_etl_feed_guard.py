"""W7-(a): гард полноты фида и флаг ``--allow-incomplete-feed``.

Инвариант: фид - полный экспорт, он может только расти. Усечённый фид
(вендорская ревизия, обрезанная выгрузка, чужой файл) не должен молча
стирать историю: аборт **до** ``DELETE``, база остаётся бит-в-бит той же.

Флаг ``--allow-incomplete-feed`` (API ``allow_incomplete_feed=True``) не
отменяет проверку, а проходит её громко: каждый обход попадает в
``LoadReport.overrides``, печатается и (в конвейере) пишется маркером в
``aw_meta``. Warn-проверки формы флагом не управляются.
"""

from __future__ import annotations

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


def row(day: str, exercise: str = "Жим штанги") -> str:
    return f"{day};10:00;Тренировка;{exercise};0;60;x;;5;90;;"


class GuardCase(unittest.TestCase):
    """Временная база на каждый тест; журнал сеется фикстурой."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = etl.connect(Path(self._dir.name) / "test.db")
        self._tmp: list[Path] = []

    def tearDown(self):
        self.conn.close()
        for path in self._tmp:
            path.unlink(missing_ok=True)
        self._dir.cleanup()

    def load(self, path: Path, **kwargs) -> etl.LoadReport:
        return etl.load_all(self.conn, journal=path, report=None, **kwargs)

    def temp_feed(self, text: str) -> Path:
        path = write_tmp(HEADER + "\n" + text)
        self._tmp.append(path)
        return path

    def snapshot(self) -> list[tuple]:
        return [
            tuple(r)
            for r in self.conn.execute(
                "SELECT * FROM sets WHERE source = ? ORDER BY source_row",
                (etl.SOURCE_JOURNAL,),
            )
        ]


class FirstLoadTests(GuardCase):
    def test_first_load_is_not_guarded(self):
        """Сравнивать не с чем: пустая база - любая история законна."""
        report = self.load(FEEDS / "journal_ok.csv")
        self.assertEqual(report.journal, 4)
        self.assertEqual(report.overrides, [])
        self.assertEqual(len(self.snapshot()), 4)


class ShrinkTests(GuardCase):
    def setUp(self):
        super().setUp()
        self.load(FEEDS / "journal_ok.csv")
        self.before = self.snapshot()

    def assertShrinks(self, path: Path, fragment: str) -> None:
        with self.assertRaises(etl.FeedShrinkError) as caught:
            self.load(path)
        message = str(caught.exception)
        self.assertIn(fragment, message)
        self.assertIn("--allow-incomplete-feed", message)
        # Главное: аборт случился ДО DELETE - строки бит-в-бит те же.
        self.assertEqual(self.snapshot(), self.before)

    def test_shrink_by_count_raises(self):
        self.assertShrinks(FEEDS / "journal_truncated.csv", "строк стало меньше")

    def test_shrink_by_tail_raises(self):
        """Строк больше, но хвост отъехал назад - это тоже усечение."""
        path = self.temp_feed(
            row("01.12.19") + "\n"
            + row("01.12.19") + "\n"
            + row("02.12.19") + "\n"
            + row("02.12.19") + "\n"
            + row("03.12.19") + "\n"
        )
        self.assertShrinks(path, "хвост истории")

    def test_shrink_by_head_raises(self):
        """Начало истории отъехало вперёд при большем числе строк."""
        path = self.temp_feed(
            row("01.01.21") + "\n"
            + row("01.01.21") + "\n"
            + row("02.01.21") + "\n"
            + row("02.01.21") + "\n"
            + row("03.01.21") + "\n"
        )
        self.assertShrinks(path, "начало истории")

    def test_message_names_the_source(self):
        with self.assertRaises(etl.FeedShrinkError) as caught:
            self.load(FEEDS / "journal_truncated.csv")
        self.assertIn(etl.SOURCE_JOURNAL, str(caught.exception))

    def test_growth_passes(self):
        """Надмножество прежнего фида - штатное обновление, не усечение."""
        path = self.temp_feed(
            row("01.01.20") + "\n"
            + row("01.01.20") + "\n"
            + row("01.01.20", "Приседания со штангой") + "\n"
            + row("03.01.20", "Жим гантелей") + "\n"
            + row("05.01.20") + "\n"
        )
        report = self.load(path)
        self.assertEqual(report.journal, 5)
        self.assertEqual(report.overrides, [])
        self.assertEqual(len(self.snapshot()), 5)


class FlagTests(GuardCase):
    def setUp(self):
        super().setUp()
        self.load(FEEDS / "journal_ok.csv")

    def test_flag_allows_shrink_and_reports_override(self):
        report = self.load(FEEDS / "journal_truncated.csv",
                           allow_incomplete_feed=True)
        self.assertEqual(report.journal, 2)
        self.assertEqual(len(self.snapshot()), 2)
        self.assertEqual(len(report.overrides), 1)
        self.assertIn("--allow-incomplete-feed", report.overrides[0])

    def test_flag_does_not_silence_warnings(self):
        """Warn не флаг-управляемы: реверс предупреждает и с флагом."""
        report = self.load(FEEDS / "journal_reversed.csv",
                           allow_incomplete_feed=True)
        self.assertTrue(any("не по возрастанию" in w for w in report.warnings))
        # Обход полноты при этом записан громко, а не проглочен.
        self.assertTrue(any("--allow-incomplete-feed" in line
                            for line in report.overrides))

    def test_flag_bypasses_header_abort_in_check_feed(self):
        shape = etl.check_feed(FEEDS / "journal_no_header.csv",
                               allow_incomplete=True)
        self.assertEqual(len(shape.overrides), 1)
        self.assertIn("заголовок", shape.overrides[0])

    def test_flag_still_requires_a_parseable_file(self):
        """Флаг снимает гард, но не делает файл разбираемым."""
        with self.assertRaises(ValueError):
            self.load(FEEDS / "journal_no_header.csv",
                      allow_incomplete_feed=True)


class ReportSourceTests(unittest.TestCase):
    """Гард полноты стоит на обоих источниках, не только на журнале."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = etl.connect(Path(self._dir.name) / "test.db")
        etl.create_schema(self.conn)
        etl.upsert(self.conn, [etl.SetRow(
            source=etl.SOURCE_REPORT, source_row=1, session_id="s1",
            date="2021-01-01", exercise="Жим штанги",
        )])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_report_shrink_raises(self):
        with self.assertRaises(etl.FeedShrinkError):
            etl.guard_completeness(self.conn, etl.SOURCE_REPORT, [])

    def test_report_shrink_with_flag_is_reported(self):
        lines = etl.guard_completeness(
            self.conn, etl.SOURCE_REPORT, [], allow_incomplete=True
        )
        self.assertEqual(len(lines), 1)
        self.assertIn(etl.SOURCE_REPORT, lines[0])


class CoverageTests(unittest.TestCase):
    def test_rows_coverage_uses_dates(self):
        rows = [
            etl.SetRow(source=etl.SOURCE_JOURNAL, source_row=1,
                       session_id="a", date="2021-01-01", exercise="Жим"),
            etl.SetRow(source=etl.SOURCE_JOURNAL, source_row=2,
                       session_id="b", date="2020-12-01", exercise="Жим"),
        ]
        coverage = etl.rows_coverage(rows, etl.SOURCE_JOURNAL)
        self.assertEqual(coverage.rows, 2)
        self.assertEqual(coverage.first_day, "2020-12-01")
        self.assertEqual(coverage.last_day, "2021-01-01")

    def test_rows_coverage_of_nothing(self):
        coverage = etl.rows_coverage([], etl.SOURCE_JOURNAL)
        self.assertEqual(coverage.rows, 0)
        self.assertIsNone(coverage.first_day)
        self.assertIsNone(coverage.last_day)


if __name__ == "__main__":
    unittest.main()
