"""Честная пустая схема и отказ среды до записи (``docs/gp-139-prereg.md``).

Витрина, которой нечего считать, не должна говорить «всё в порядке»:
``digest`` обещал «тренируйся по плану» без единого канала, ``outliers`` -
«выбросов нет» на пустом журнале, ``return`` - план для упражнения «?».
Здесь прибиты три вещи: предикат «источник пуст» (§5.1), отказы витрин
(§5.2, Р3) и то, что ``load`` больше не оставляет на диске пустой файл
базы после отказа (Р4). Живые пины «sets непуст» живут в ``test_cli.py``
и не трогаются: предикат срабатывает только на ноль строк.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from koilog import cli, etl
from koilog.core import one_rm
from koilog.health import digest, state as health_state

ROWS = [
    ("s1", "2024-01-10", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 30.0, 10),
    ("s1", "2024-01-10", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 30.0, 8),
    ("s2", "2024-01-20", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 32.0, 10),
    ("s2", "2024-01-20", "Скручивания", "ПРЕСС", 20.0, 12),
]


class _Base(unittest.TestCase):
    """Временная база: схема ``sets`` есть всегда, строки - по флагу."""

    rows = ROWS

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.executescript(etl.SCHEMA)
        conn.executescript(etl.WEEKLY_KOI_VIEW)
        for i, (session, date, name, group, weight, reps) in enumerate(
                self.rows, 1):
            conn.execute(
                """
                INSERT INTO sets (source, source_row, session_id, date,
                                  exercise, exercise_group, equipment,
                                  set_index, weight, reps, rm)
                     VALUES ('journal', ?, ?, ?, ?, ?, 'гантели', 1, ?, ?, ?)
                """,
                (i, session, date, name, group, weight, reps,
                 one_rm(weight, reps)),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._dir.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ("--db", str(self.db)) + argv
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class EmptySetsBase(_Base):
    """``sets`` есть, но пуст; health-таблиц нет вовсе."""

    rows = []


class PopulatedBase(_Base):
    """Рабочий сценарий «журнал разобран, часов ещё нет»."""

    rows = ROWS


class SourcePredicateTests(unittest.TestCase):
    """§5.1: «источник пуст» = таблицы нет **или** в ней ноль строк."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(Path(self._dir.name) / "p.db")
        self.conn.execute("CREATE TABLE есть (x INTEGER)")
        self.conn.execute("INSERT INTO есть (x) VALUES (1)")
        self.conn.execute("CREATE TABLE пусто (x INTEGER)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_missing_table_is_empty(self):
        self.assertTrue(cli._source_empty(self.conn, "нет_такой"))

    def test_table_without_rows_is_empty(self):
        self.assertTrue(cli._source_empty(self.conn, "пусто"))

    def test_table_with_a_row_is_not_empty(self):
        self.assertFalse(cli._source_empty(self.conn, "есть"))

    def test_any_empty_table_makes_the_source_empty(self):
        """Несколько таблиц - «пусто», если пуста хотя бы одна."""
        self.assertTrue(cli._source_empty(self.conn, "есть", "пусто"))
        self.assertFalse(cli._source_empty(self.conn, "есть", "есть"))


class RefusalFormTests(EmptySetsBase):
    """§5.3/§6 п.4,6,11: отказ - одна строка, код 1, без трассировки."""

    def assert_one_line_refusal(self, argv: tuple[str, ...]) -> str:
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 1)
        text = out.strip()
        self.assertTrue(text, "отказ должен быть виден")
        self.assertEqual(len(text.splitlines()), 1,
                         f"ждали одну строку, получили: {text!r}")
        self.assertIn("нет данных", text)
        self.assertIn("python -m koilog load", text)
        self.assertNotIn("Traceback", text + err)
        return text

    def test_outliers_refuses_empty_journal(self):
        text = self.assert_one_line_refusal(("outliers",))
        self.assertNotIn("выбросов нет", text)

    def test_return_refuses_empty_journal(self):
        text = self.assert_one_line_refusal(("return",))
        self.assertNotIn("возврат к весам", text)
        self.assertNotIn("=== возврат к весам: ? ===", text)

    def test_outliers_csv_mode_refuses_too(self):
        code, out, _ = self.run_cli("outliers", "--csv")
        self.assertEqual(code, 1)
        self.assertNotIn("rule,sets", out, "CSV не начинается без данных")


class DigestEmptySchemaTests(EmptySetsBase):
    """§5.2/§5.3/§6 п.1: пусто всё - отказ, а не «можно по плану»."""

    def test_refuses_when_both_health_and_journal_are_empty(self):
        code, out, err = self.run_cli("digest")
        self.assertEqual(code, 1)
        text = out.strip()
        self.assertEqual(len(text.splitlines()), 1, text)
        self.assertIn("нет данных", text)
        for step in ("python -m koilog load", "apple-load", "health-load"):
            self.assertIn(step, text)
        self.assertNotIn("тренируйся по плану", text)
        self.assertNotIn("можно по плану", text)
        self.assertNotIn("Traceback", out + err)

    def test_json_mode_refuses_without_fake_recommendation(self):
        code, out, _ = self.run_cli("digest", "--json")
        self.assertEqual(code, 1)
        self.assertNotIn("recommendation", out)


class DigestNoHoursTests(PopulatedBase):
    """§5.2/§5.3/§6 п.2: журнал есть, часов нет - карточка честная, код 0."""

    def test_card_stays_but_says_no_data(self):
        code, out, _ = self.run_cli("digest")
        self.assertEqual(code, 0, "рабочий сценарий не должен отказывать")
        self.assertIn("дневной дайджест", out)
        self.assertIn("состояние: нет данных", out)
        self.assertIn("рекомендация: нет данных", out)
        self.assertNotIn("тренируйся по плану", out)
        self.assertNotIn("штатный план", out)

    def test_state_line_prints_no_data_instead_of_the_plan(self):
        self.assertIn("состояние: нет данных", self.run_cli("digest")[1])

    def test_json_carries_the_honest_reason(self):
        code, out, _ = self.run_cli("digest", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["state"]["reason"], "нет данных")
        self.assertEqual(payload["recommendation"]["level"], "nodata")
        self.assertIn("apple-load", payload["recommendation"]["text"])


class ReadyPlanRegressionTests(unittest.TestCase):
    """§6 п.3: когда данные есть, «можно по плану» остаётся (не заглушка)."""

    class _ReadyGate:
        ready = True
        window_only = False
        reasons = ()

    def test_normal_state_still_offers_the_plan(self):
        load = {"available": True, "date": "2024-01-10",
                "days_since_training": 1}
        rec = digest.recommend([], load, self._ReadyGate())
        self.assertEqual(rec["title"], "можно по плану")
        self.assertIn("тренируйся по плану", rec["text"])

    def test_normal_state_line_is_still_the_plan(self):
        """Гарда узкая: причина «штатный план» остаётся «штатным планом»."""
        day = health_state.StateDay(
            day="2024-01-10", state=health_state.STATE_NORMAL,
            reason="штатный план")
        self.assertEqual(digest._state_line(day.to_dict()), "штатный план")

    def test_no_data_state_line_is_not_the_plan(self):
        day = health_state.StateDay(
            day="?", state=health_state.STATE_NORMAL, reason="нет данных")
        self.assertEqual(digest._state_line(day.to_dict()), "нет данных")


class LoadPreflightTests(unittest.TestCase):
    """Р4/§6 п.8: отказ среды - до записи, файла базы не остаётся."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_sources_do_not_create_the_db(self):
        target = self.dir / "новая.db"
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["--db", str(target), "load",
                             "--journal", str(self.dir / "нет1.csv"),
                             "--report", str(self.dir / "нет2.csv")])
        self.assertEqual(code, 1)
        self.assertFalse(target.exists(),
                         "отказ среды не должен оставлять пустой файл базы")
        self.assertIn("нет1.csv", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_missing_report_only_does_not_create_the_db(self):
        journal = self.dir / "journal.csv"
        journal.write_text("шапка\n", encoding="utf-8")
        target = self.dir / "новая.db"
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["--db", str(target), "load",
                             "--journal", str(journal),
                             "--report", str(self.dir / "нет.csv")])
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())
        self.assertIn("нет.csv", err.getvalue())


if __name__ == "__main__":
    unittest.main()
