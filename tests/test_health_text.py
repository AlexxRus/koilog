"""Тесты выравнивания таблиц: числа вправо, текст влево (раунд 5)."""

from __future__ import annotations

import unittest

from koilog.health import text


class TableAlignTests(unittest.TestCase):
    def test_numbers_right_aligned(self):
        out = text.table(["имя", "n"], [["a", 1], ["b", 1000]])
        self.assertEqual(
            out.splitlines(),
            ["имя     n", "-" * 9, "a       1", "b    1000"],
        )

    def test_text_left_aligned(self):
        out = text.table(["имя"], [["abc"], ["x"]])
        self.assertEqual(
            out.splitlines(),
            ["имя", "-" * 3, "abc", "x  "],
        )

    def test_neutral_dash_does_not_break_numeric_column(self):
        # «—» не ломает числовую колонку: числа уходят вправо.
        out = text.table(["эффект"], [["+0.35"], ["—"], ["-1.20"]])
        body = out.splitlines()[2:]
        self.assertEqual([ln.strip() for ln in body], ["+0.35", "—", "-1.20"])
        self.assertTrue(body[0].endswith("+0.35"))
        self.assertTrue(body[2].endswith("-1.20"))

    def test_dates_stay_left(self):
        out = text.table(["дата"], [["2024-02-04"], ["2024-11-30"]])
        body = out.splitlines()[2:]
        self.assertTrue(all(ln.startswith("2024-") for ln in body))

    def test_explicit_align_override(self):
        out = text.table(["x"], [["1"], ["22"]], align="l")
        body = out.splitlines()[2:]
        self.assertEqual([ln.rstrip() for ln in body], ["1", "22"])

    def test_percent_and_degree_count_as_numbers(self):
        out = text.table(["p", "t"], [["63.6 %", "36.8 °C"]])
        self.assertEqual(
            out.splitlines(),
            ["     p        t", "-" * 15, "63.6 %  36.8 °C"],
        )

    def test_empty_rows_returns_empty(self):
        self.assertEqual(text.table(["a", "b"], []), "")

    def test_none_becomes_blank(self):
        out = text.table(["a", "b"], [["x", None]])
        self.assertEqual(out.splitlines()[2].rstrip(), "x")


if __name__ == "__main__":
    unittest.main()
