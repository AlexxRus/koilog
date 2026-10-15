"""Командный интерфейс: ``python -m koilog <команда>``.

Команды:

``load``
    Разобрать оба CSV в базу и напечатать отчёт о качестве.
``stats``
    Сводка по упражнениям: объём, лучший и последний 1RM, динамика.
``plan``
    Программа роста нагрузки, посчитанная от последней тренировки.
``weeks``
    Недельные суммы КОИ по упражнениям.
``history``
    История упражнения за интервал дат ``--from``…``--to``; ``--reverse``
    разворачивает порядок от последней даты к первой.
``explain``
    Разбор парсинга выгрузок и расчёта по упражнению, подход за подходом.
``check``
    Проблемные данные: нераспознанные упражнения, подозрительное время,
    неправдоподобная добавка, оценка веса тела, блок выбросов.
``outliers``
    Отдельный разбор выбросов: сколько подходов помечено каждым правилом
    и какие именно.
``export``
    Выгрузка таблицы ``sets`` в CSV.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from . import equipment, etl, rules, stats
from .core import BODY_WEIGHT

__all__ = ["build_parser", "main"]


# --------------------------------------------------------------------------
# Консоль
# --------------------------------------------------------------------------


def _setup_console() -> None:
    """Научить консоль печатать кириллицу.

    Кодовая страница Windows по умолчанию (866, 1251) не понимает ни
    ``→``, ни ``ё``, а весь вывод здесь русский. Сначала просим систему
    переключить кодовую страницу вывода на UTF-8, затем переключаем сам
    поток. Если не выйдет - печатаем с заменой непечатаемых символов,
    чтобы не падать с ``UnicodeEncodeError`` посреди отчёта.
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # pragma: no cover - зависит от окружения
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    """Открыть базу, объяснив, если она ещё не разобрана.

    ``etl.connect`` создаёт файл, если его нет, - но таблицу в нём
    создаёт ``load``. Без этой проверки ``python -m koilog stats`` до первой
    загрузки падал бы с голым ``no such table: sets`` и трассировкой.
    """
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(
            f"нет файла базы: {path}\n"
            f"сначала разберите выгрузки:  python -m koilog load"
        )
    conn = etl.connect(path)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sets'"
    ).fetchone()
    if row is None:
        conn.close()
        raise SystemExit(
            f"в базе {path} нет таблицы sets\n"
            f"скорее всего она не разобрана:  python -m koilog load"
        )
    return conn


def _csv_number(value: float | None, digits: int = 2) -> str:
    """Число для CSV: с округлением, ``None`` - пустой ячейкой.

    Отдельная функция, а не ``f"{value or '':g}"``: во-первых, ``0.0``
    должен печататься нулём, а не пустотой; во-вторых, из базы приходит
    17 знаков после запятой, и в таблице это нечитаемо.
    """
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _parse_year_value(text: str) -> tuple[int, float | None]:
    """Разобрать ``ГОД=КГ`` для ``--bodyweight-year``.

    ``None`` вместо числа означает «в этом году вес взять из данных» - так
    можно вернуть конкретный год к автоматической оценке, не трогая
    остальные переопределения.
    """
    year, _, value = text.partition("=")
    try:
        parsed_year = int(year.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"ожидалось «ГОД=КГ», получено {text!r}"
        ) from None
    value = value.strip()
    if not value or value.lower() == "none":
        return parsed_year, None
    try:
        return parsed_year, float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"вес тела в «{text}» не число"
        ) from None


def _year_overrides(pairs: list[tuple[int, float | None]] | None) -> dict[int, float]:
    """Свернуть повторы ``--bodyweight-year`` в карту ``год -> вес``.

    Ключи со значением ``None`` отбрасываются: ``_refill_bodyweight`` сам
    знает, что для года без переопределения надо взять оценку из данных.
    """
    if not pairs:
        return {}
    return {year: value for year, value in pairs if value is not None}


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    """Напечатать таблицу, выровняв колонки по фактической ширине."""
    if not rows:
        return
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for row in cells:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)))


#: Подпись колонок отчёта ``stats`` - чтобы вывод читался, не заглядывая в код.
_STATS_LEGEND = (
    "\nколонки: подх - подходов в окне; лучший - лучший 1RM окна; "
    "рекорд - лучший 1RM за всю историю;\n"
    "         посл. - 1RM последней тренировки; Δ к рекорду = посл. − рекорд "
    "(0 = на рекорде);\n"
    "         МА30 - скользящее среднее 1RM за 30 дней по упражнению "
    "(база КОИ); объём = вес x повторы."
)


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


def _drop_legacy(conn) -> str | None:
    """Убрать таблицу ``workouts`` от прежней схемы базы.

    Она полностью пересобирается из тех же CSV, поэтому держать её рядом с
    ``sets`` - ловушка: одинаковое имя упражнения, разные колонки, разные
    единицы веса. Удаляем и говорим об этом.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workouts'"
    ).fetchone()
    if row is None:
        return None
    count = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
    conn.execute("DROP TABLE workouts")
    conn.commit()
    return f"удалена устаревшая таблица workouts ({count} строк)"


def cmd_load(args: argparse.Namespace) -> int:
    conn = etl.connect(args.db)
    try:
        dropped = _drop_legacy(conn)
        if dropped and not args.quiet:
            print(dropped)
        overrides = _year_overrides(args.bodyweight_year)
        fallback = args.bodyweight if args.bodyweight is not None else args.body_weight
        report = etl.load_all(
            conn,
            journal=args.journal,
            report=args.report,
            body_weight=fallback,
            journal_wins=not args.report_wins,
            bodyweight_by_year=overrides or None,
        )
        applied = etl.bodyweight_map(conn, overrides or None, fallback=fallback)
        if args.quiet:
            print(f"{report.total}")
            return 0

        print(f"загружено подходов: {report.total} "
              f"(журнал {report.journal}, отчёт {report.report})")
        if report.skipped_dates:
            print(f"пропущено дат из отчёта: {len(report.skipped_dates)} "
                  f"(есть в журнале): {', '.join(report.skipped_dates)}")
        print()
        _print_bodyweight(applied)
        _print_summary(conn)
    finally:
        conn.close()

    print()
    return cmd_check(argparse.Namespace(db=args.db, limit=15, quiet=False))


def _print_bodyweight(applied: dict[int, float]) -> None:
    """Показать, какой вес тела взят для каждого года.

    Без этой строки замена константы 70 на оценку из данных выглядит как
    магия: в базе появляются другие 1RM у подтягиваний и отжиманий на
    брусьях, и объяснить, откуда взялись 82 вместо 70, нечем.
    """
    if not applied:
        return
    print("вес тела по годам (из записей total_bodyweight, "
          "переопределения — из --bodyweight-year):")
    for year in sorted(applied):
        print(f"    {year}: {applied[year]:g} кг")
    print()


def _print_summary(conn) -> None:
    data = etl.summary(conn)
    headers = ("источник", "подходов", "тренировок", "дней", "с", "по",
               "с 1RM", "на времени", "пропущено")
    rows = []
    for name, block in data["by_source"].items():
        rows.append((
            name, block["sets"], block["sessions"], block["days"],
            block["first_day"], block["last_day"],
            block["with_rm"], block["timed"], block["skipped"],
        ))
    total = data["total"]
    rows.append((
        "всего", total["sets"], total["sessions"], total["days"],
        total["first_day"], total["last_day"],
        total["with_rm"], total["timed"], total["skipped"],
    ))
    _print_table(headers, rows)
    print()
    print(f"упражнений: {total['exercises']}   групп: {total['groups']}   "
          f"дней: {total['days']} "
          f"({total['first_day']} ... {total['last_day']})")
    if total["without_rm"]:
        print(f"без 1RM: {total['without_rm']} подходов "
              f"(своё тело без веса, работа на время)")


def cmd_stats(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        since = None if args.all else stats.since_for_months(conn, args.months)
        rows = stats.exercise_report(
            conn,
            exercise=args.exercise,
            group=args.group,
            min_sets=args.min_sets,
            with_rm_only=args.with_rm,
            order=args.order,
            limit=args.limit,
            since=since,
        )
        total = len(stats.exercise_report(conn))
    finally:
        conn.close()

    if not rows:
        print("под фильтр ничего не попало")
        return 1
    if args.csv:
        print("exercise,group,sets,sessions,best_rm,best_day,record_rm,last_rm,"
              "delta_to_record,ma_30d,volume")
        for r in rows:
            print(",".join((
                r.exercise, r.exercise_group, str(r.sets), str(r.sessions),
                _csv_number(r.best_rm), r.best_day or "",
                _csv_number(r.record_rm), _csv_number(r.last_rm),
                _csv_number(r.delta_to_record), _csv_number(r.ma_30d),
                _csv_number(r.volume, digits=1),
            )))
        return 0

    print(stats.format_report(rows))
    print(_STATS_LEGEND)
    if since:
        print(f"окно: последние {args.months} мес. до последней тренировки, "
              f"с {since}")
        print(f"показано {len(rows)} из {total} упражнений "
              f"(вся история - ключ --all)")
    else:
        print(f"показано {len(rows)} из {total}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    extra = {} if args.count is None else {"count": args.count}
    conn = _open(args)
    try:
        body_weight = _bodyweight_for_plan(conn, args)
        plan = stats.progression_from_db(
            conn,
            args.exercise,
            hands=args.hands,
            sets=args.sets,
            steps=args.steps,
            use_db_reference=args.db_reference,
            vest=args.vest,
            body_weight=body_weight,
            optimal_reps=args.optimal_reps,
            **extra,
        )
        period_rows = stats.periods(conn, args.exercise)
    finally:
        conn.close()

    if args.csv:
        _print_plan_csv(plan, period_rows)
        return 0

    print(stats.format_plan(plan))
    wave = stats.format_transitions(plan.wave)
    if wave:
        print(wave)
    print(stats.format_periods(period_rows))
    # Упёршись в край списка весов - не ошибка: программа построена,
    # просто расти больше некуда. Ошибка - когда построить нечего.
    return 0 if plan.built else 1


def _bodyweight_for_plan(conn, args: argparse.Namespace) -> float | None:
    """Вес тела для лестницы «тело плюс жилет», если он вообще нужен.

    Порядок приоритета: явный ``--body-weight``, затем переопределение года
    из ``--bodyweight-year``, затем оценка из записей за последний год.
    Если пользователь сказал «в 2021 вес был 82», это и должно попасть в
    лестницу, а не мода из журнала.
    """
    if args.body_weight is not None:
        return args.body_weight
    year = _latest_year(conn)
    overrides = _year_overrides(args.bodyweight_year)
    if year in overrides:
        return overrides[year]
    return etl.bodyweight_map(conn).get(year)


def _latest_year(conn) -> int:
    row = conn.execute("SELECT MAX(date) FROM sets").fetchone()
    return int(row[0][:4]) if row and row[0] else 0


def _print_plan_csv(plan, period_rows) -> None:
    """Числа вместо графики: кому нужен график, тот открывает это в Excel."""
    print("exercise,group,step,sets,weight,reps,load,reference_rm,koi,band")
    for step in plan.program:
        sets = " ".join(f"{s.weight:g}x{s.reps}" for s in step.sets)
        print(
            f"{plan.stats.exercise},{plan.stats.exercise_group},{step.index},"
            f"\"{sets}\",{step.weight:g},{step.reps},"
            f"{_csv_number(step.load, digits=4)},"
            f"{_csv_number(step.reference_rm)},{_csv_number(step.koi)},"
            f"{step.band}"
        )
    print()
    print("period,days,since,until,sessions,sets,volume,best_rm,prev_rm,"
          "delta,koi_avg")
    for row in period_rows:
        print(
            f"{row.name},{row.days},{row.since},{row.until},"
            f"{row.sessions},{row.sets},{_csv_number(row.volume, digits=1)},"
            f"{_csv_number(row.best_rm)},{_csv_number(row.prev_rm)},"
            f"{_csv_number(row.delta)},{_csv_number(row.koi_average)}"
        )


def cmd_weeks(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        rows = stats.weekly_koi(
            conn, exercise=args.exercise, group=args.group, limit=args.limit
        )
    finally:
        conn.close()

    if not rows:
        print("недель с посчитанным КОИ нет")
        return 1
    print(stats.format_weekly_koi(rows), end="")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        rows = stats.history(
            conn, args.exercise, since=args.since, until=args.until,
            limit=args.limit, reverse=args.reverse,
        )
        total = len(
            stats.history(conn, args.exercise, since=args.since, until=args.until)
        )
    finally:
        conn.close()

    if args.csv:
        print("date,session_id,top_weight,top_reps,top_rm,sets,reps_total,"
              "volume,rm_session,rm_delta,koi_sum,band")
        for r in rows:
            print(",".join((
                r.date, r.session_id, _csv_number(r.top_weight),
                "" if r.top_reps is None else str(r.top_reps),
                _csv_number(r.top_rm), str(r.sets), str(r.reps_total),
                _csv_number(r.volume, digits=1), _csv_number(r.rm_session),
                _csv_number(r.rm_delta), _csv_number(r.koi_sum), r.band or "",
            )))
        return 0

    print(stats.format_history(
        rows, args.exercise, since=args.since, until=args.until,
        reverse=args.reverse,
    ))
    if args.limit and total > len(rows):
        print(f"показано {len(rows)} из {total} тренировок "
              f"(--limit 0 - показать все)")
    return 0 if rows else 1


def cmd_explain(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        audit = etl.exercise_audit(conn, args.exercise)
        sets = stats.explain_sets(conn, args.exercise, limit=args.sets)
    finally:
        conn.close()

    print("=== как читаются выгрузки ===")
    for note in etl.parse_notes():
        print(f"  - {note}")

    if audit is None:
        print()
        print(f"упражнение «{args.exercise}» в базе не найдено: "
              f"проверьте название (полный список - python -m koilog stats).")
        return 1

    print()
    print(f"=== разбор «{audit.exercise}» ===")
    print(f"подходов всего: {audit.total}, с 1RM: {audit.with_rm}")
    print(f"на времени: {audit.timed}, пропущено: {audit.skipped}, "
          f"разминок: {audit.warmup}, выбросов: {audit.outlier}")
    print(f"период: {audit.first_day} … {audit.last_day}")
    print(f"источники: {', '.join(audit.sources)}")
    if audit.raw_low is not None:
        print(f"записанный вес: {audit.raw_low:g} … {audit.raw_high:g} кг")
    modes = ", ".join(f"{name} ({count})" for name, count in audit.weight_modes)
    print(f"режимы веса: {modes}")
    if audit.outlier_rules:
        rules = ", ".join(f"{name} ({count})" for name, count in audit.outlier_rules)
        print(f"сработавшие правила: {rules}")
    else:
        print("сработавшие правила: нет")

    print()
    print("=== последние подходы: от сырого значения до КОИ ===")
    if not sets:
        print("подходов нет")
        return 1
    print(stats.format_explain(sets), end="")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        report = etl.quality_report(conn)
        gaps = _stock_gaps(conn)
    finally:
        conn.close()
    text = report.format(limit=args.limit)
    if gaps:
        text += _format_stock_gaps(gaps, limit=args.limit)
    print(text, end="")
    print("подробный разбор выбросов:  python -m koilog outliers")
    return 0


def cmd_outliers(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        groups = etl.outlier_summary(conn)
        marked, total = etl.outlier_totals(conn)
        seconds = etl.reinterpreted_as_seconds(conn)
        detail = etl.outlier_detail(
            conn, rule=args.rule, limit=args.limit if not args.csv else 0
        )
        gaps = _stock_gaps(conn)
    finally:
        conn.close()

    if args.csv:
        print("rule,sets,exercises,weight_low,weight_high,first_day,last_day")
        for group in groups:
            print(
                f"{group.rule},{group.sets},{group.exercises},"
                f"{_csv_number(group.weight_low)},{_csv_number(group.weight_high)},"
                f"{group.first_day},{group.last_day}"
            )
        print()
        print("rule,date,exercise,group,weight,reps,time_seconds")
        for row in detail:
            print(",".join("" if v is None else str(v) for v in row))
        if seconds:
            print()
            print("rule,exercise,sets,first_day,last_day")
            for exercise, sets, first_day, last_day in seconds:
                print(f"seconds_in_weight,{exercise},{sets},{first_day},{last_day}")
        if gaps:
            print()
            print("rule,exercise,weights")
            for exercise, weights in gaps:
                print(f"not_in_stock,{exercise},"
                      f"{' '.join(f'{w:g}' for w in weights)}")
        return 0

    if not marked:
        print("выбросов нет: все подходы прошли проверку правил")
    else:
        share = marked / total * 100 if total else 0.0
        print(f"выбросов: {marked} подходов из {total} ({share:.1f} %)")
        print()
        _print_table(
            ("правило", "подходов", "упражнений", "вес", "период"),
            [
                (
                    group.rule,
                    group.sets,
                    group.exercises,
                    (
                        "—"
                        if group.weight_low is None
                        else (
                            f"{group.weight_low:g}"
                            if group.weight_low == group.weight_high
                            else f"{group.weight_low:g}…{group.weight_high:g}"
                        )
                    ),
                    (
                        "—"
                        if not group.first_day
                        else f"{group.first_day} … {group.last_day}"
                    ),
                )
                for group in groups
            ],
        )

    if seconds:
        print()
        print(_format_seconds(seconds))

    if detail:
        print()
        print("подходы:")
        _print_table(
            ("правило", "дата", "упражнение", "группа", "вес", "повторов",
             "секунд"),
            detail,
        )

    print()
    _print_rule_table()

    if gaps:
        print()
        print(_format_stock_gaps(gaps))
    return 0


def _print_rule_table() -> None:
    """Список правил - и сработавших, и тех, что не поймали ничего.

    Пустое правило в списке нужно: блок анализа должен показывать, что
    «повторов больше 30» проверено и не найдено, а не молчать об этом.
    """
    print("правила:")
    for rule in rules.RULES:
        print(f"  {rule.name:20} {rule.title}")
        print(f"  {'':20} {rule.hint}")


def _format_seconds(rows: list[tuple]) -> str:
    """Блок «вес прочитан как секунды».

    Отдельный блок, а не строка в таблице выбросов: эти подходы не выброшены,
    а разобраны - вес убран, время записано. Смешивать их с выбросами
    нельзя, иначе получится «197 подходов выкинуто», хотя на самом деле
    ни одна строка не потеряна.
    """
    total = sum(row[1] for row in rows)
    lines = [
        "",
        f"ВЕС, ПРОЧИТАННЫЙ КАК СЕКУНДЫ ({total})",
        "подходы не выброшены: записанный вес перенесён в «секунды»",
    ]
    for exercise, sets, first_day, last_day in rows:
        lines.append(f"    {exercise[:44]:<44} {sets:>5}   {first_day} … {last_day}")
    return "\n".join(lines)


def _stock_gaps(conn) -> list[tuple[str, list[float]]]:
    """Записанные веса, которых нет в инвентаре, по упражнениям."""
    weights_by_exercise: dict[str, list[float]] = {}
    for row in conn.execute(
        """
        SELECT exercise, weight
          FROM sets
         WHERE is_outlier = 0
           AND skipped = 0
           AND is_timed = 0
           AND weight IS NOT NULL
           AND weight > 0
        """
    ):
        weights_by_exercise.setdefault(row["exercise"], []).append(row["weight"])

    gaps: list[tuple[str, list[float]]] = []
    for exercise, weights in sorted(weights_by_exercise.items()):
        missing = equipment.stock_gaps(exercise, weights)
        if missing:
            gaps.append((exercise, missing))
    return gaps


def _format_stock_gaps(
    gaps: list[tuple[str, list[float]]],
    *,
    limit: int | None = None,
) -> str:
    """Блок «журнал и зал разошлись».

    В настоящих данных таких упражнений 62, и полный список закрывает собой
    всё остальное, поэтому в ``check`` печатается только начало, а полный
    список остаётся в ``outliers``.
    """
    shown = gaps if limit is None else gaps[:limit]
    lines = [
        "",
        f"ВЕСА, КОТОРЫХ НЕТ В ИНВЕНТАРЕ ({len(gaps)} упражнений)",
        "подходы настоящие и остаются в базе, но предложить такой вес вперёд "
        "нельзя:",
    ]
    for exercise, weights in shown:
        profile = equipment.profile_of(exercise)
        lines.append(
            f"    {exercise[:44]:<44} {', '.join(f'{w:g}' for w in weights[:12])}"
            f"{' …' if len(weights) > 12 else ''}   [{profile.id}]"
        )
    if len(shown) < len(gaps):
        lines.append(f"    … ещё {len(gaps) - len(shown)}: "
                     f"python -m koilog outliers")
    return "\n".join(lines) + "\n"


def cmd_export(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        written = etl.export_csv(conn, args.out)
    finally:
        conn.close()
    print(f"выгружено {written} строк в {args.out}")
    return 0


# --------------------------------------------------------------------------
# Разбор аргументов
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m koilog",
        description="Журнал силовых тренировок: разбор CSV, статистика, "
                    "программа роста нагрузки.",
    )
    parser.add_argument(
        "--db", default=etl.DEFAULT_DB,
        help=f"файл базы (по умолчанию {etl.DEFAULT_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="разобрать CSV в базу")
    p_load.add_argument("--journal", default=etl.DEFAULT_JOURNAL)
    p_load.add_argument("--report", default=etl.DEFAULT_REPORT)
    p_load.add_argument(
        "--body-weight", type=float, default=BODY_WEIGHT,
        help=f"вес собственного тела для годов без данных (по умолчанию "
             f"{BODY_WEIGHT:g}); для остальных годов берётся из записей",
    )
    p_load.add_argument(
        "--bodyweight", type=float, default=None,
        help="задать вес тела сразу для всех лет, где нет своих данных",
    )
    p_load.add_argument(
        "--bodyweight-year", type=_parse_year_value, action="append",
        default=None, metavar="ГОД=КГ",
        help="переопределить вес тела для года: 2021=82; "
             "значение none означает «взять из данных». Можно повторять",
    )
    p_load.add_argument(
        "--report-wins", action="store_true",
        help="при совпадении дат брать отчёт, а не журнал (по умолчанию "
             "журнал)",
    )
    p_load.add_argument("--quiet", action="store_true",
                        help="вывести только количество подходов")
    p_load.set_defaults(func=cmd_load)

    p_stats = sub.add_parser("stats", help="сводка по упражнениям")
    p_stats.add_argument("--exercise", help="только это упражнение")
    p_stats.add_argument("--group", help="только эта группа упражнений")
    p_stats.add_argument("--min-sets", type=int, default=0,
                         help="отбросить упражнения с меньшим числом подходов")
    p_stats.add_argument("--with-rm", action="store_true",
                         help="только те, где 1RM вообще считается")
    p_stats.add_argument(
        "--order", default="best_rm",
        choices=("best_rm", "last_rm", "last_delta", "exercise", "sets"),
    )
    p_stats.add_argument("--limit", type=int, default=25)
    p_stats.add_argument("--months", type=int, default=3,
                         help="окно в месяцах от последней тренировки "
                              "(по умолчанию 3)")
    p_stats.add_argument("--all", action="store_true",
                         help="вся история без окна")
    p_stats.add_argument("--csv", action="store_true", help="вывод в CSV")
    p_stats.set_defaults(func=cmd_stats)

    p_plan = sub.add_parser("plan", help="программа роста нагрузки")
    p_plan.add_argument("exercise", help="название упражнения как в базе")
    p_plan.add_argument("--sets", type=int, default=3, help="подходов в шаге")
    p_plan.add_argument("--steps", type=int, default=18, help="шагов в программе")
    p_plan.add_argument("--count", type=int, default=None,
                        help="переходов в сводке (по умолчанию 13)")
    p_plan.add_argument("--hands", type=int, default=None,
                        help="во сколько рук считать вес; по умолчанию берётся "
                             "из инвентаря (2 для гантелей в двух руках)")
    p_plan.add_argument("--vest", action="store_true",
                        help="разрешить жилет и лестницу «тело плюс жилет»: "
                             "в журнале жилета нет, по умолчанию его нет и "
                             "в инвентаре")
    p_plan.add_argument("--body-weight", dest="body_weight", type=float,
                        default=None,
                        help="вес собственного тела для лестницы «тело плюс "
                             "жилет»; по умолчанию берётся из записей последнего "
                             "года")
    p_plan.add_argument("--bodyweight-year", type=_parse_year_value,
                        action="append", default=None, metavar="ГОД=КГ")
    p_plan.add_argument("--optimal-reps", action="store_true",
                        help="подбирать максимум повторов на новом весе; по "
                             "умолчанию каждый новый вес начинается с 6 "
                             "повторов («волна со сбросом»)")
    p_plan.add_argument("--csv", action="store_true",
                        help="вывод в CSV: шаги программы и периоды")
    p_plan.add_argument("--db-reference", action="store_true",
                        help="база КОИ - скользящее среднее 1RM за 30 дней "
                             "из базы, а не 1RM стартового подхода")
    p_plan.set_defaults(func=cmd_plan)

    p_weeks = sub.add_parser("weeks", help="недельные суммы КОИ по упражнениям")
    p_weeks.add_argument("--exercise", help="только это упражнение")
    p_weeks.add_argument("--group", help="только эта группа упражнений")
    p_weeks.add_argument("--limit", type=int, default=20)
    p_weeks.set_defaults(func=cmd_weeks)

    p_history = sub.add_parser(
        "history", help="история упражнения за интервал дат"
    )
    p_history.add_argument("exercise", help="название упражнения как в базе")
    p_history.add_argument(
        "--from", dest="since", metavar="ДАТА", default=None,
        help="начало интервала, ISO ГГГГ-ММ-ДД, включительно "
             "(по умолчанию - с первой записи)",
    )
    p_history.add_argument(
        "--to", dest="until", metavar="ДАТА", default=None,
        help="конец интервала, ISO ГГГГ-ММ-ДД, включительно "
             "(по умолчанию - по последнюю запись)",
    )
    p_history.add_argument("--limit", type=int, default=200,
                           help="сколько тренировок показать (0 - все)")
    p_history.add_argument(
        "--reverse", action="store_true",
        help="порядок от последней даты к первой; при --limit остаются "
             "последние (самые свежие) тренировки (по умолчанию - от первой "
             "к последней)",
    )
    p_history.add_argument("--csv", action="store_true", help="вывод в CSV")
    p_history.set_defaults(func=cmd_history)

    p_explain = sub.add_parser(
        "explain", help="разбор парсинга и расчёта по упражнению"
    )
    p_explain.add_argument("exercise", help="название упражнения как в базе")
    p_explain.add_argument("--sets", type=int, default=5,
                           help="сколько последних подходов расписать по шагам")
    p_explain.set_defaults(func=cmd_explain)

    p_check = sub.add_parser("check", help="проблемные данные")
    p_check.add_argument("--limit", type=int, default=20)
    p_check.add_argument("--quiet", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_outliers = sub.add_parser("outliers", help="разбор выбросов по правилам")
    p_outliers.add_argument("--rule", help="только это правило из koilog.rules")
    p_outliers.add_argument("--limit", type=int, default=20,
                            help="сколько подходов показать (0 - все)")
    p_outliers.add_argument("--csv", action="store_true")
    p_outliers.set_defaults(func=cmd_outliers)

    p_export = sub.add_parser("export", help="выгрузка в CSV")
    p_export.add_argument("--out", default=etl.DEFAULT_EXPORT)
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        # Опечатка в пути к выгрузке - это не повод показывать трассировку
        # в три экрана, а готовый сам отчёт о том, что не так.
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
