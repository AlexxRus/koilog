"""W7-(f): синтетические фиды обязаны воспроизводить все классы аномалий.

Реальный ``data/journal.csv.broken-reversed-20261022`` (1.44 МБ) в git не
кладётся - он остаётся уликой в реестре пост-мортема (§10.5). В git идёт
синтетика: мелкая, но с теми же классами - обратный порядок, пустые
разделители, строки-заметки с эмодзи, вариант без заголовка, вариант
заголовка, усечённый фид.

Тесты здесь **независимы** от проверок ветки (d): они пинят сами фикстуры,
а не поведение ``check_feed``. Иначе фикстура и проверка могли бы «согласиться
оба на ошибке».
"""

from __future__ import annotations

import csv
import io
import unittest
from pathlib import Path

from koilog import etl

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "tests" / "fixtures" / "feeds"

#: Канонический заголовок журнала, 12 колонок.
CANONICAL = [
    "ДАТА", "ВРЕМЯ", "ТРЕНИРОВКА", "УПРАЖНЕНИЕ", "ДИСТАНЦИЯ КМ", "ВЕС",
    "МНОЖИТЕЛЬ", "ВРЕМЯ", "ПОВТОРЫ", "ОТДЫХ", "ПРОПУЩЕН", "ПРИМЕЧАНИЯ",
]


def read_feed(name: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Вернуть ``(физические строки, записи после заголовка, заголовок)``."""
    text = (FEEDS / name).read_text(encoding="utf-8")
    with io.StringIO(text) as handle:
        reader = csv.reader(handle, delimiter=";")
        header = next(reader)
        records = list(reader)
    return text.splitlines(), records, header


def is_blank(record: list[str]) -> bool:
    return not record or not any(field.strip() for field in record)


def data_dates(records: list[list[str]]) -> list[str]:
    return [etl.parse_journal_date(r[0]) for r in records if not is_blank(r)]


def inversions(dates: list[str]) -> int:
    return sum(1 for a, b in zip(dates, dates[1:]) if a > b)


class FixtureContractTests(unittest.TestCase):
    """Каждая фикстура несёт свой класс аномалии (и только его профиль)."""

    def test_ok_fixture_is_clean(self):
        lines, records, header = read_feed("journal_ok.csv")
        self.assertEqual([c.upper() for c in header], CANONICAL)
        self.assertEqual(len(records), 4)
        self.assertEqual(sum(1 for r in records if is_blank(r)), 0)
        self.assertEqual(inversions(data_dates(records)), 0)
        self.assertEqual(len(list(etl.load_journal(FEEDS / "journal_ok.csv"))), 4)

    def test_reversed_fixture_has_inversion(self):
        """Класс 1: обратный порядок дат (плюс пустой разделитель)."""
        _, records, _ = read_feed("journal_reversed.csv")
        self.assertGreaterEqual(inversions(data_dates(records)), 1)
        self.assertGreaterEqual(sum(1 for r in records if is_blank(r)), 1)

    def test_blanks_fixture_exceeds_one_percent(self):
        """Класс 2: пустые разделители - плотность выше порога 1%."""
        lines, records, _ = read_feed("journal_blanks.csv")
        blanks = sum(1 for r in records if is_blank(r))
        self.assertGreaterEqual(blanks, 1)
        self.assertGreaterEqual(blanks / len(lines), 0.01)

    def test_notes_fixture_has_emoji_note_row(self):
        """Класс 3: строка-заметка - упражнения нет, в заметке эмодзи."""
        _, records, header = read_feed("journal_notes.csv")
        self.assertEqual([c.upper() for c in header], CANONICAL)
        notes = [
            r for r in records
            if not is_blank(r) and not r[3].strip()
            and any(ord(ch) > 0x1F000 for field in r for ch in field)
        ]
        self.assertEqual(len(notes), 1)
        # Строка-заметка не даёт подхода, но загрузка остальных не ломается.
        self.assertEqual(len(list(etl.load_journal(FEEDS / "journal_notes.csv"))), 2)

    def test_no_header_fixture_starts_with_data(self):
        """Класс 4: заголовка нет - первая строка уже данные."""
        lines, _, first = read_feed("journal_no_header.csv")
        self.assertTrue(lines)
        self.assertNotEqual([c.upper() for c in first[:4]], CANONICAL[:4])
        with self.assertRaises(ValueError):
            list(etl.load_journal(FEEDS / "journal_no_header.csv"))

    def test_header_variant_fixture_keeps_first_four(self):
        """Класс 5: имена колонок другие при том же числе - парсинг позиционный."""
        _, records, header = read_feed("journal_header_variant.csv")
        self.assertEqual([c.strip().upper() for c in header[:4]], CANONICAL[:4])
        self.assertNotEqual([c.strip().upper() for c in header], CANONICAL)
        self.assertEqual(len(header), len(CANONICAL))
        self.assertEqual(len(list(etl.load_journal(FEEDS / "journal_header_variant.csv"))), 2)

    def test_truncated_fixture_covers_only_the_tail(self):
        """Класс 6: усечённый фид - только последние дни и меньше строк."""
        _, records, _ = read_feed("journal_truncated.csv")
        dates = data_dates(records)
        self.assertEqual(len(set(dates)), 2)
        _, ok_records, _ = read_feed("journal_ok.csv")
        self.assertGreater(min(dates), max(data_dates(ok_records)))
        self.assertLess(len(dates), len(data_dates(ok_records)))

    def test_every_fixture_has_twelve_columns(self):
        """Форма одна на все фикстуры: 12 колонок, разделитель ``;``."""
        for path in sorted(FEEDS.glob("*.csv")):
            with self.subTest(fixture=path.name):
                text = path.read_text(encoding="utf-8")
                for number, line in enumerate(text.splitlines(), start=1):
                    if not line.strip():
                        continue
                    self.assertEqual(
                        len(line.split(";")), 12,
                        f"{path.name}:{number}: не 12 колонок",
                    )


if __name__ == "__main__":
    unittest.main()
