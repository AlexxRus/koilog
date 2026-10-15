"""Тесты koilog.cli: разбор аргументов, коды возврата, формат вывода.

Команды проверяются на маленькой временной базе, а не на настоящей:
CLI - это тонкий слой, и интересно в нём только то, что легко сломать
незаметно - коды возврата и разбор аргументов.
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from koilog import cli, etl, stats
from koilog.core import one_rm


ROWS = [
    # (session, date, exercise, group, weight, reps)
    ("s1", "2024-01-10", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 30.0, 10),
    ("s1", "2024-01-10", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 30.0, 8),
    ("s2", "2024-01-20", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 32.0, 10),
    ("s2", "2024-01-20", "Скручивания", "ПРЕСС", 20.0, 12),
]


class CliFixture(unittest.TestCase):
    """База в файле: CLI ходит по пути, а не по соединению."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.executescript(etl.SCHEMA)
        conn.executescript(etl.WEEKLY_KOI_VIEW)
        for i, (session, date, name, group, weight, reps) in enumerate(ROWS, 1):
            conn.execute(
                """
                INSERT INTO sets (source, source_row, session_id, date,
                                  exercise, exercise_group, equipment,
                                  set_index, weight, reps, rm)
                     VALUES ('journal', ?, ?, ?, ?, ?, 'гантели', 1, ?, ?, ?)
                """,
                (i, session, date, name, group, weight, reps, one_rm(weight, reps)),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._dir.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Прогнать команду, вернуть (код, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        argv = ("--db", str(self.db)) + argv
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# Разбор аргументов
# --------------------------------------------------------------------------


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_command_is_required(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                self.parser.parse_args([])

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                self.parser.parse_args(["ерунда"])

    def test_db_is_global_and_comes_before_the_command(self):
        args = self.parser.parse_args(["--db", "x.db", "stats"])
        self.assertEqual(args.db, "x.db")
        self.assertEqual(args.command, "stats")

    def test_plan_requires_exercise(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                self.parser.parse_args(["plan"])

    def test_unknown_order_is_rejected(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                self.parser.parse_args(["stats", "--order", "вес"])

    def test_defaults(self):
        args = self.parser.parse_args(["stats"])
        self.assertEqual(args.order, "best_rm")
        self.assertEqual(args.limit, 25)
        self.assertEqual(args.min_sets, 0)
        self.assertFalse(args.with_rm)
        self.assertFalse(args.csv)

    def test_plan_defaults(self):
        args = self.parser.parse_args(["plan", "Жим"])
        self.assertEqual(args.sets, 3)
        self.assertEqual(args.steps, 18)
        self.assertIsNone(args.hands)      # None - не угадывать
        self.assertIsNone(args.count)      # None - взять значение по умолчанию
        self.assertFalse(args.db_reference)

    def test_load_report_wins_is_off_by_default(self):
        self.assertFalse(self.parser.parse_args(["load"]).report_wins)

    def test_flags_parse(self):
        args = self.parser.parse_args(
            ["stats", "--with-rm", "--min-sets", "5", "--limit", "3", "--csv"]
        )
        self.assertTrue(args.with_rm)
        self.assertTrue(args.csv)
        self.assertEqual(args.min_sets, 5)
        self.assertEqual(args.limit, 3)


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------


class CsvNumberTests(unittest.TestCase):
    def test_none_is_empty(self):
        self.assertEqual(cli._csv_number(None), "")

    def test_zero_is_zero_not_empty(self):
        """``0.0 or ''`` - вот как ноль превращается в пустую ячейку."""
        self.assertEqual(cli._csv_number(0.0), "0.00")

    def test_rounds_to_two_by_default(self):
        self.assertEqual(cli._csv_number(146.11764705882354), "146.12")

    def test_digits_are_configurable(self):
        self.assertEqual(cli._csv_number(522110.0, digits=1), "522110.0")

    def test_no_float_noise(self):
        self.assertNotIn("e", cli._csv_number(0.000001, digits=8).lower())


class DropLegacyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def test_nothing_to_drop(self):
        self.assertIsNone(cli._drop_legacy(self.conn))

    def test_drops_and_reports_the_count(self):
        self.conn.execute("CREATE TABLE workouts (exercise TEXT)")
        self.conn.executemany(
            "INSERT INTO workouts VALUES (?)", [("Жим",), ("Тяга",)]
        )
        self.conn.commit()
        message = cli._drop_legacy(self.conn)
        self.assertIn("workouts", message)
        self.assertIn("2", message)
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        self.assertNotIn("workouts", tables)

    def test_is_idempotent(self):
        self.conn.execute("CREATE TABLE workouts (exercise TEXT)")
        self.conn.commit()
        self.assertIsNotNone(cli._drop_legacy(self.conn))
        self.assertIsNone(cli._drop_legacy(self.conn))


class ConsoleTests(unittest.TestCase):
    def test_reconfigure_is_called_and_survives_failure(self):
        """Поток без reconfigure (подмена в тестах) не должен ронять CLI."""
        import sys

        original = sys.stdout
        sys.stdout = io.StringIO()          # у StringIO нет reconfigure
        try:
            cli._setup_console()
        finally:
            sys.stdout = original

    def test_broken_pipe_style_errors_do_not_propagate(self):
        class Hostile:
            encoding = "utf-8"

            def reconfigure(self, **kwargs):
                raise OSError("поток закрыт")

        for name in ("__stdout__", "__stderr__"):
            original = getattr(sys_module, name)
            setattr(sys_module, name, Hostile())
            try:
                cli._setup_console()
            finally:
                setattr(sys_module, name, original)


import sys as sys_module  # используется в ConsoleTests


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


class StatsCommandTests(CliFixture):
    def test_prints_a_table_and_the_count(self):
        code, out, _ = self.run_cli("stats", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn("Жим гантелей лежа", out)
        self.assertIn("показано 1 из 2", out)

    def test_csv_mode_has_header_and_row_per_exercise(self):
        code, out, _ = self.run_cli("stats", "--csv")
        self.assertEqual(code, 0)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)          # шапка + два упражнения
        self.assertTrue(lines[0].startswith("exercise,group,sets"))
        self.assertIn("Жим гантелей лежа", lines[1])

    def test_csv_numbers_are_rounded(self):
        _, out, _ = self.run_cli("stats", "--csv", "--exercise", "Скручивания")
        line = [ln for ln in out.splitlines() if "Скручивания" in ln][0]
        for cell in line.split(","):
            self.assertNotIn("e", cell.lower())
        self.assertIn(".", line)

    def test_group_filter(self):
        _, out, _ = self.run_cli("stats", "--group", "ПРЕСС")
        self.assertIn("Скручивания", out)
        self.assertNotIn("Жим гантелей лежа", out)

    def test_empty_result_is_an_error(self):
        code, out, _ = self.run_cli("stats", "--exercise", "Нет такого")
        self.assertEqual(code, 1)
        self.assertIn("ничего не попало", out)

    def test_min_sets(self):
        _, out, _ = self.run_cli("stats", "--min-sets", "3")
        self.assertIn("Жим гантелей лежа", out)
        self.assertNotIn("Скручивания", out)

    # -- окно «последние три месяца» -------------------------------------

    def _add_old_exercise(self):
        """Старое упражнение: последняя тренировка задолго до окна."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date,
                              exercise, exercise_group, equipment,
                              set_index, weight, reps, rm)
                 VALUES ('journal', 999, 's0', '2020-05-01',
                         'Тяга блока', 'СПИНА', 'блок', 1, 50.0, 10, 60.0)
            """
        )
        conn.commit()
        conn.close()

    def test_window_is_on_by_default_and_hides_old_exercises(self):
        self._add_old_exercise()
        _, out, _ = self.run_cli("stats", "--all")
        self.assertIn("Тяга блока", out)
        _, out, _ = self.run_cli("stats")
        self.assertNotIn("Тяга блока", out)
        self.assertIn("Жим гантелей лежа", out)

    def test_window_prints_its_start_date(self):
        _, out, _ = self.run_cli("stats")
        # последняя тренировка фикстуры 2024-01-20, окно с 2023-10-20
        self.assertIn("окно", out)
        self.assertIn("с 2023-10-20", out)

    def test_window_footer_counts_all_exercises(self):
        self._add_old_exercise()
        _, out, _ = self.run_cli("stats")
        self.assertIn("из 3 упражнений", out)

    def test_all_shows_the_plain_footer(self):
        _, out, _ = self.run_cli("stats", "--all")
        self.assertNotIn("окно", out)
        self.assertIn("показано 2 из 2", out)

    def test_months_option_widens_the_window(self):
        self._add_old_exercise()
        _, out, _ = self.run_cli("stats", "--months", "1000")
        self.assertIn("Тяга блока", out)


class PlanCommandTests(CliFixture):
    def test_prints_a_program(self):
        code, out, _ = self.run_cli("plan", "Жим гантелей лежа")
        self.assertEqual(code, 0)
        self.assertIn("программа", out)
        self.assertIn("Жим гантелей лежа", out)

    def test_running_out_of_weights_is_not_a_failure(self):
        """32 - последний вес в базе; упереться в край - не ошибка."""
        code, out, _ = self.run_cli("plan", "Жим гантелей лежа")
        self.assertEqual(code, 0)
        self.assertIn("верх списка снарядов", out)

    def test_unknown_exercise_is_an_error(self):
        code, out, _ = self.run_cli("plan", "Нет такого")
        self.assertEqual(code, 1)
        self.assertIn("не найдено", out)

    def test_sets_option_changes_the_step(self):
        """format_plan выравнивает колонки, поэтому длины строк равны -
        считать надо сами подходы в шаге, а не символы."""
        def sets_in_first_step(out: str) -> int:
            for line in out.splitlines():
                if "x" in line and line.strip()[:1].isdigit():
                    return line.count("x")
            self.fail(f"не нашлось ни одной строки программы:\n{out}")

        _, one, _ = self.run_cli("plan", "Жим гантелей лежа", "--sets", "1")
        _, three, _ = self.run_cli("plan", "Жим гантелей лежа", "--sets", "3")
        self.assertEqual(sets_in_first_step(one), 1)
        self.assertEqual(sets_in_first_step(three), 3)

    def test_steps_option_limits_the_output(self):
        _, few, _ = self.run_cli("plan", "Жим гантелей лежа", "--steps", "2")
        bands = ("Crazy", "Light", "Optimal", "Load", "Лёгкий", "Тяжёлый")
        self.assertEqual(sum(few.count(b) for b in bands), 2)


class CheckCommandTests(CliFixture):
    def test_prints_all_four_sections(self):
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        for heading in ("нераспознанные упражнения", "подозрительные",
                        "неправдоподобная добавка", "вес тела по годам",
                        "всего замечаний"):
            self.assertIn(heading, out)

    def test_clean_data_reports_zero_problems(self):
        _, out, _ = self.run_cli("check")
        self.assertIn("всего замечаний: 0", out)


class CheckCommandLimitTests(CliFixture):
    """Та же команда, но на базе с настоящими замечаниями.

    В основной фикстуре всё чисто, и проверка усечения печати там
    пропускалась бы - то есть ничего не проверяла.
    """

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        for i, (name, weight, added, mode) in enumerate(
            [
                ("Подтягивания на перекладине", 31.7, -38.3, "total_bodyweight"),
                ("Подтягивания на перекладине", 25.0, -45.0, "total_bodyweight"),
                ("Подтягивания на перекладине", 30.0, -40.0, "total_bodyweight"),
            ],
            start=100,
        ):
            conn.execute(
                """
                INSERT INTO sets (source, source_row, session_id, date,
                                  exercise, exercise_group, set_index, weight,
                                  added_weight, reps, weight_mode)
                     VALUES ('report', ?, 'r1', '2018-04-27', ?,
                             'ТЯГА ВЕРТИКАЛЬНАЯ', 1, ?, ?, 5, ?)
                """,
                (i, name, weight, added, mode),
            )
        conn.commit()
        conn.close()

    def test_limit_truncates_lines_but_not_the_total(self):
        conn = etl.connect(self.db)
        try:
            problems = etl.quality_report(conn).problems
        finally:
            conn.close()
        self.assertEqual(problems, 3)

        _, full, _ = self.run_cli("check", "--limit", "100")
        _, short, _ = self.run_cli("check", "--limit", "1")
        self.assertLess(len(short), len(full))
        # сумма в конце - настоящая, а не число напечатанных строк
        self.assertIn("всего замечаний: 3", full)
        self.assertIn("всего замечаний: 3", short)
        self.assertEqual(short.count("2018-04-27"), 1)
        self.assertEqual(full.count("2018-04-27"), 3)


class OutliersCommandTests(CliFixture):
    """Отдельный разбор выбросов: `koilog check` на них только ссылается."""

    _marked = 0

    def _mark(self, rule, weight, exercise="Отжимания", group="ОТЖИМАНИЯ"):
        OutliersCommandTests._marked += 1
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, weight, reps, is_outlier,
                              outlier_rule, added_weight)
                 VALUES ('journal', ?, 's9', '2024-03-01', ?, ?, ?, 10, 1, ?,
                         NULL)
            """,
            (900 + OutliersCommandTests._marked, exercise, group, weight, rule),
        )
        conn.commit()
        conn.close()

    def test_clean_base_says_there_are_no_outliers(self):
        code, out, _ = self.run_cli("outliers")
        self.assertEqual(code, 0)
        self.assertIn("выбросов нет", out)

    def test_marked_row_shows_the_rule_and_the_count(self):
        self._mark("pushups_max", 45.0)
        code, out, _ = self.run_cli("outliers")
        self.assertEqual(code, 0)
        self.assertIn("выбросов: 1 подходов", out)
        self.assertIn("pushups_max", out)
        self.assertIn("Отжимания", out)

    def test_rules_without_hits_are_still_listed(self):
        """Проверенное и оказавшееся чистым тоже должно быть видно."""
        _, out, _ = self.run_cli("outliers")
        for rule in ("seconds_in_weight", "pushups_max", "bodyweight_max",
                     "reps_max"):
            self.assertIn(rule, out)

    def test_rule_filter_keeps_one_rule(self):
        self._mark("pushups_max", 45.0)
        self._mark("reps_max", 10.0)
        _, out, _ = self.run_cli(
            "outliers", "--rule", "reps_max", "--limit", "0"
        )
        # сводка и правила показывают всё, а детали - только запрошенное
        self.assertIn("подходы:", out)
        detail = out.split("подходы:")[-1].split("правила:")[0]
        self.assertIn("reps_max", detail)
        self.assertNotIn("pushups_max", detail)

    def test_csv_mode_prints_a_header(self):
        self._mark("pushups_max", 45.0)
        _, out, _ = self.run_cli("outliers", "--csv", "--limit", "0")
        self.assertTrue(out.startswith("rule,sets,exercises,weight_low"))
        self.assertIn("rule,date,exercise,group,weight,reps,time_seconds", out)
        self.assertIn("pushups_max,2024-03-01,Отжимания,ОТЖИМАНИЯ,45.0", out)

    def test_seconds_block_is_not_called_an_outlier(self):
        """Разобранные записи - не выбросы, их считать надо отдельно."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, is_timed, time_seconds,
                              timed_reason)
                 VALUES ('journal', 901, 's9', '2024-03-02',
                         'Подъемы ног в висе', 'ПОДЪЁМ НОГ', 1, 45, ?)
            """,
            (etl.SECONDS_TIMED_REASON,),
        )
        conn.commit()
        conn.close()
        _, out, _ = self.run_cli("outliers")
        self.assertIn("выбросов нет", out)
        self.assertIn("ВЕС, ПРОЧИТАННЫЙ КАК СЕКУНДЫ", out)
        self.assertIn("не выброшены", out)
        self.assertIn("Подъемы ног в висе", out)

    def test_stock_gaps_are_listed(self):
        """Вес, которого нет в инвентаре, попадает в тот же отчёт.

        32 кг суммарных - это 16 на руку, а 16 нет среди 17 пар.
        """
        code, out, _ = self.run_cli("outliers")
        self.assertEqual(code, 0)
        self.assertIn("ВЕСА, КОТОРЫХ НЕТ В ИНВЕНТАРЕ", out)
        self.assertIn("Жим гантелей лежа", out)
        self.assertIn("32", out)
        self.assertIn("[dumbbell_2]", out)

    def test_in_stock_weights_are_not_reported_as_gaps(self):
        """30 кг суммарных - это 15 на руку, такая пара есть."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, weight, reps)
                 VALUES ('journal', 950, 's9', '2024-03-03',
                         'Жим гантелей лежа', 'ЖИМ ЛЁЖА', 30.0, 8)
            """
        )
        conn.commit()
        conn.close()
        _, out, _ = self.run_cli("outliers")
        gaps = out.split("ВЕСА, КОТОРЫХ НЕТ В ИНВЕНТАРЕ")[-1]
        self.assertIn("32", gaps)
        self.assertNotIn("30", gaps)


class BodyweightYearTests(unittest.TestCase):
    """Разбор ``--bodyweight-year``: он кормит и загрузку, и план."""

    def test_year_and_weight(self):
        self.assertEqual(cli._parse_year_value("2021=82"), (2021, 82.0))

    def test_decimal_weight(self):
        self.assertEqual(cli._parse_year_value("2018=67.4"), (2018, 67.4))

    def test_none_means_take_it_from_the_data(self):
        self.assertEqual(cli._parse_year_value("2021=none"), (2021, None))
        self.assertEqual(cli._parse_year_value("2021=None"), (2021, None))

    def test_empty_value_is_the_same_as_none(self):
        self.assertEqual(cli._parse_year_value("2021="), (2021, None))

    def test_bad_year_is_rejected(self):
        with self.assertRaises(Exception):
            cli._parse_year_value("вчера=82")

    def test_bad_weight_is_rejected(self):
        with self.assertRaises(Exception):
            cli._parse_year_value("2021=столько")

    def test_none_pairs_do_not_become_overrides(self):
        """``2021=none`` - это не «вес = None», а отсутствие override."""
        self.assertEqual(cli._year_overrides([(2021, None)]), {})

    def test_repeated_flag_collects_years(self):
        parser = cli.build_parser()
        args = parser.parse_args(
            ["load", "--bodyweight-year", "2021=82",
             "--bodyweight-year", "2022=none"]
        )
        self.assertEqual(cli._year_overrides(args.bodyweight_year), {2021: 82.0})


class PlanFlagsTests(CliFixture):
    def test_vest_is_off_by_default(self):
        parser = cli.build_parser()
        self.assertFalse(parser.parse_args(["plan", "Жим"]).vest)

    def test_optimal_reps_is_off_by_default(self):
        """Волна со сбросом на 6 повторов - осмысленный выбор по умолчанию."""
        parser = cli.build_parser()
        self.assertFalse(parser.parse_args(["plan", "Жим"]).optimal_reps)

    def test_csv_flag_parses(self):
        parser = cli.build_parser()
        self.assertTrue(parser.parse_args(["plan", "Жим", "--csv"]).csv)

    def test_plan_prints_periods_and_charts(self):
        _, out, _ = self.run_cli("plan", "Жим гантелей лежа")
        self.assertIn("период", out)
        self.assertIn("объём", out)
        self.assertIn("изменение 1RM", out)

    def test_plan_csv_has_both_blocks(self):
        """Кому ASCII не годится, тот получает те же числа в CSV."""
        _, out, _ = self.run_cli("plan", "Жим гантелей лежа", "--csv")
        self.assertTrue(out.startswith("exercise,group,step"))
        self.assertIn("period,days,since,until,sessions", out)

    def test_plan_csv_has_a_row_per_step(self):
        _, text, _ = self.run_cli("plan", "Жим гантелей лежа", "--csv")
        steps = [
            line for line in text.splitlines()
            if line.startswith("Жим гантелей лежа,")
        ]
        self.assertEqual(len(steps), 18)

    def test_optimal_reps_changes_the_program(self):
        _, wave, _ = self.run_cli("plan", "Жим гантелей лежа")
        _, walk, _ = self.run_cli(
            "plan", "Жим гантелей лежа", "--optimal-reps"
        )
        self.assertNotEqual(wave, walk)


class WeeksCommandTests(CliFixture):
    def test_no_koi_is_an_error(self):
        """В фикстуре koi не считается - недель нет."""
        code, out, _ = self.run_cli("weeks")
        self.assertEqual(code, 1)
        self.assertIn("недель", out)


class ExportCommandTests(CliFixture):
    def test_writes_all_rows(self):
        target = Path(self._dir.name) / "out.csv"
        code, out, _ = self.run_cli("export", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertIn("4", out)
        self.assertTrue(target.exists())
        with io.open(target, encoding="utf-8-sig") as handle:
            self.assertEqual(len(handle.read().splitlines()), 5)  # шапка + 4

    def test_export_carries_derived_columns(self):
        target = Path(self._dir.name) / "out.csv"
        self.run_cli("export", "--out", str(target))
        text = target.read_text(encoding="utf-8-sig")
        for column in ("rm", "rm_session", "rm_delta", "rm_ma_30d", "koi"):
            self.assertIn(column, text.splitlines()[0])


class MainTests(CliFixture):
    def test_returns_the_command_code(self):
        code, _, _ = self.run_cli("stats", "--exercise", "Нет такого")
        self.assertEqual(code, 1)

    def test_missing_db_says_to_run_load(self):
        """Раньше был голый ``no such table: sets`` с трассировкой."""
        target = Path(self._dir.name) / "нет-такой.db"
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stderr(err):
                cli.main(["--db", str(target), "stats"])
        message = str(caught.exception)
        self.assertIn("python -m koilog load", message)
        self.assertFalse(target.exists(), "база не должна создаваться на чтение")

    def test_db_without_sets_table_says_to_run_load(self):
        target = Path(self._dir.name) / "пустая.db"
        sqlite3.connect(target).close()          # файл есть, таблицы sets нет
        with self.assertRaises(SystemExit) as caught:
            with redirect_stderr(io.StringIO()):
                cli.main(["--db", str(target), "stats"])
        self.assertIn("python -m koilog load", str(caught.exception))

    def test_read_commands_refuse_an_unloaded_db(self):
        for command in (["stats"], ["weeks"], ["check"],
                        ["plan", "Жим"], ["export"]):
            with self.subTest(command=command[0]):
                with self.assertRaises(SystemExit):
                    with redirect_stderr(io.StringIO()):
                        cli.main(["--db", str(Path(self._dir.name) / "нет.db"),
                                  *command])

    def test_load_does_create_the_db(self):
        """load - единственная команда, которой база нужна несуществующей."""
        target = Path(self._dir.name) / "создастся.db"
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["--db", str(target), "load",
                             "--journal", str(Path(self._dir.name) / "нет1.csv"),
                             "--report", str(Path(self._dir.name) / "нет2.csv")])
        # не «сначала разберите выгрузки», а честная ошибка про путь
        self.assertEqual(code, 1)
        self.assertIn("нет1.csv", err.getvalue())
        self.assertTrue(target.exists(), "база создаётся до чтения выгрузок")

    def test_bad_path_is_a_message_not_a_traceback(self):
        """Опечатка в пути - не повод показывать трассировку в три экрана."""
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["--db", str(self.db), "load",
                             "--journal", str(Path(self._dir.name) / "нет.csv")])
        self.assertEqual(code, 1)
        self.assertIn("нет.csv", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
