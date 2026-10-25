"""Статистика по упражнениям и программа, построенная из базы.

Модуль читает готовую таблицу ``sets`` и отвечает на два вопроса:

* что сделано по каждому упражнению - объём, лучший 1RM, динамика, КОИ
  (коэффициент относительной интенсивности) (``exercise_report``);
* что делать дальше - программа роста нагрузки, стартованная от последней
  реальной тренировки (``progression_from_db``).

В отличие от ручных расчётов, здесь ничего не набирается руками:
стартовый вес и повторы берутся из базы, список доступных снарядов - из
инвентаря (:mod:`koilog.equipment`), а сами расчёты делегированы в
:mod:`koilog.core`, который тестируется без БД.

Что не попадает в метрики
-------------------------

Пропущенные подходы, подходы на времени и подходы, помеченные правилом из
:mod:`koilog.rules`, в статистику не входят. Раньше это было не так: 110 кг
суммарного веса тела на брусьях давали 1RM 243 кг, а 822 подхода
отжиманий с «процентом от веса тела» портили среднее по всей группе.
Сами строки остаются в базе и в ``koilog export`` - их можно посмотреть и
разобрать, просто они не участвуют в расчётах.

Про инвентарь
-------------

Список доступных весов берётся из :mod:`koilog.equipment`, а не из истории
тренировок: программа не имеет права предложить вес, которого в зале нет.
Записанный вес при этом может лежать вне лестницы (82 кг у отжиманий,
44 кг у тяги штанги в наклоне), поэтому он приводится к ближайшему
снаряду не тяжелее, а расхождение печатается отдельной строкой.

Оговорка о формуле КОИ
---------------------

При интенсивности, близкой к 100 %, знаменатель формулы уходит в ноль и КОИ
взлетает до тысяч, поэтому в базе стоит порог 98 % (``etl._KOI_UPDATE``).
КОИ считается **по каждому упражнению отдельно** - и за тренировку
(``v_session_koi``), и за неделю (``v_weekly_koi``): у жима и тяги в одной
группе разные базы 1RM, и общая сумма теряла бы масштаб.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import equipment, taxonomy
from .core import (
    KOI_ABOVE_WEEK,
    KOI_BANDS_WEEK,
    TRANSITIONS,
    Progression,
    TransitionPlan,
    koi_band,
    progression,
    snap_to_ladder,
    transitions,
)

__all__ = [
    "ExerciseStats",
    "PERIOD_DAYS",
    "PeriodStats",
    "ProgramPlan",
    "RecentExercise",
    "WeeklyKoi",
    "WeeklyKoiGroup",
    "HistoryRow",
    "ascii_bar",
    "available_weights",
    "exercise_report",
    "explain_sets",
    "format_explain",
    "format_history",
    "format_periods",
    "format_plan",
    "format_recent_exercises",
    "format_report",
    "format_transitions",
    "format_weekly_koi",
    "format_weekly_koi_by_group",
    "history",
    "periods",
    "progression_from_available",
    "progression_from_db",
    "recent_exercises",
    "since_for_months",
    "weekly_koi",
    "weekly_koi_by_group",
]


# --------------------------------------------------------------------------
# Статистика по упражнениям
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExerciseStats:
    """Сводка по одному упражнению за всю историю.

    1RM берётся по лучшему подходу **за тренировку** (то есть
    ``rm_session``), а не по отдельному подходу: иначе четыре одинаковых
    подхода одного упражнения в одной тренировке давали бы четыре разных
    значения и «рекорд» подвигался бы на пустом месте.
    """

    exercise: str
    exercise_group: str
    equipment: str
    sessions: int
    sets: int
    reps_total: int
    volume: float
    first_day: str
    last_day: str
    sources: str
    timed_sets: int
    koi_total: float
    best_day: str | None = None
    best_rm: float | None = None
    best_weight: float | None = None
    best_reps: int | None = None
    last_day_rm: str | None = None
    last_rm: float | None = None
    last_weight: float | None = None
    last_reps: int | None = None
    last_delta: float | None = None
    ma_30d: float | None = None
    record_rm: float | None = None
    prev_day: str | None = None
    prev_rm: float | None = None

    @property
    def best_set(self) -> str:
        """``60 кг x 5 -> 1RM 67.5`` или ``нет данных``."""
        if self.best_rm is None:
            return "нет данных"
        return f"{self.best_weight:g} кг x {self.best_reps} -> {self.best_rm:.1f}"

    @property
    def last_set(self) -> str:
        if self.last_rm is None:
            return "нет данных"
        return f"{self.last_weight:g} кг x {self.last_reps} -> {self.last_rm:.1f}"

    @property
    def delta_to_record(self) -> float | None:
        """Насколько последний 1RM не дотягивает до рекорда.

        ``0`` означает «на рекорде», отрицательное число - «ниже рекорда на
        столько-то кг», положительное возможно только если рекорд считался по
        другой базе и встречается в старых базах. Именно это число печатает
        колонка «Δ к рекорду»: раньше на её месте была дельта к предыдущей
        тренировке, и она обнулялась на двух похожих тренировках подряд.
        """
        if self.last_rm is None or self.record_rm is None:
            return None
        return self.last_rm - self.record_rm

    @property
    def progress(self) -> float | None:
        """Насколько выросла разница между последним и предыдущим 1RM."""
        return self.last_delta

    @property
    def koi_average(self) -> float:
        return self.koi_total / self.sets if self.sets else 0.0

    @property
    def is_going_up(self) -> bool | None:
        if self.last_delta is None:
            return None
        return self.last_delta > 0


#: Сводные показатели по упражнению. Считаются в два прохода: сначала
#: агрегаты, потом лучший и последний 1RM оконными функциями - в одном
#: SELECT'е «максимум + последняя строка» не выразить.
#: ``{since}`` подставляется как условие даты (см. :func:`exercise_report`).
_TOTALS_SQL = """
SELECT exercise,
       MIN(exercise_group)                AS exercise_group,
       MIN(equipment)                     AS equipment,
       COUNT(DISTINCT session_id)         AS sessions,
       COUNT(*)                           AS sets,
       COALESCE(SUM(reps), 0)             AS reps_total,
       COALESCE(SUM(weight * reps), 0.0)  AS volume,
       MIN(date)                          AS first_day,
       MAX(date)                          AS last_day,
       GROUP_CONCAT(DISTINCT source)      AS sources,
       COALESCE(SUM(is_timed), 0)         AS timed_sets,
       COALESCE(SUM(koi), 0.0)            AS koi_total
  FROM sets
 WHERE skipped = 0
   AND is_outlier = 0
   {since}
 GROUP BY exercise
"""

#: Один подход - одна строка; лучший 1RM тренировки считается через
#: ``MAX(rm)`` в группе ``(exercise, date, session_id)``.
#: Слой ``days`` и ``shifted`` считаются по **всей** истории: дельта
#: последней тренировки должна отсчитываться от предыдущей, даже если она
#: уже за границей окна. Фильтр ``{since}`` стоит в ``ranked`` - лучший и
#: последний 1RM выбираются уже внутри окна.
_BEST_LAST_SQL = """
WITH days AS (
    SELECT exercise,
           date,
           session_id,
           MAX(rm) AS rm_day
      FROM sets
     WHERE rm IS NOT NULL
     GROUP BY exercise, date, session_id
),
shifted AS (
    SELECT exercise,
           date,
           session_id,
           rm_day,
           rm_day - LAG(rm_day) OVER (
               PARTITION BY exercise
               ORDER BY date, session_id
           ) AS delta
      FROM days
),
ranked AS (
    SELECT exercise,
           date,
           session_id,
           rm_day,
           delta,
           ROW_NUMBER() OVER (
               PARTITION BY exercise
               ORDER BY rm_day DESC, date ASC, session_id ASC
           ) AS rn_best,
           ROW_NUMBER() OVER (
               PARTITION BY exercise
               ORDER BY date DESC, rm_day DESC, session_id DESC
           ) AS rn_last
      FROM shifted
     {since}
),
picked AS (
    SELECT exercise,
           MAX(CASE WHEN rn_best = 1 THEN date       END) AS best_day,
           MAX(CASE WHEN rn_best = 1 THEN rm_day     END) AS best_rm,
           MAX(CASE WHEN rn_best = 1 THEN session_id END) AS best_session,
           MAX(CASE WHEN rn_last = 1 THEN date       END) AS last_day,
           MAX(CASE WHEN rn_last = 1 THEN rm_day     END) AS last_rm,
           MAX(CASE WHEN rn_last = 1 THEN session_id END) AS last_session,
           MAX(CASE WHEN rn_last = 1 THEN delta      END) AS last_delta,
           MAX(CASE WHEN rn_last = 2 THEN date       END) AS prev_day,
           MAX(CASE WHEN rn_last = 2 THEN rm_day     END) AS prev_rm
      FROM ranked
     GROUP BY exercise
),
records AS (
    -- Рекорд - лучший 1RM за **всю** историю упражнения, а не за окно:
    -- именно с ним сравнивается последняя тренировка в колонке «Δ к рекорду».
    SELECT exercise, MAX(rm_day) AS record_rm
      FROM days
     GROUP BY exercise
)
SELECT p.exercise,
       p.best_day,
       p.best_rm,
       p.last_day  AS last_day_rm,
       p.last_rm,
       p.last_delta,
       (SELECT r.record_rm FROM records r WHERE r.exercise = p.exercise) AS record_rm,
       p.prev_day,
       p.prev_rm,
       (SELECT s.weight FROM sets s
         WHERE s.exercise = p.exercise
           AND s.session_id = p.best_session
           AND s.rm = p.best_rm
         ORDER BY s.set_index
         LIMIT 1)  AS best_weight,
       (SELECT s.reps FROM sets s
         WHERE s.exercise = p.exercise
           AND s.session_id = p.best_session
           AND s.rm = p.best_rm
         ORDER BY s.set_index
         LIMIT 1)  AS best_reps,
       (SELECT s.weight FROM sets s
         WHERE s.exercise = p.exercise
           AND s.session_id = p.last_session
           AND s.rm = p.last_rm
         ORDER BY s.set_index
         LIMIT 1)  AS last_weight,
       (SELECT s.reps FROM sets s
         WHERE s.exercise = p.exercise
           AND s.session_id = p.last_session
           AND s.rm = p.last_rm
         ORDER BY s.set_index
         LIMIT 1)  AS last_reps,
       (SELECT s.rm_ma_30d FROM sets s
         WHERE s.exercise = p.exercise
           AND s.date = p.last_day
         ORDER BY s.id
         LIMIT 1)  AS ma_30d
  FROM picked p
"""


def exercise_report(
    conn: sqlite3.Connection,
    *,
    exercise: str | None = None,
    group: str | None = None,
    min_sets: int = 0,
    with_rm_only: bool = False,
    order: str = "best_rm",
    limit: int | None = None,
    since: str | None = None,
) -> list[ExerciseStats]:
    """Сводка по упражнениям, отсортированная по заданному признаку.

    ``since`` (ISO-дата) - начало окна: учитываются только подходы с этой
    даты, и лучший/последний 1RM выбираются внутри окна. Дельта последней
    тренировки при этом по-прежнему отсчитывается от тренировки за окном -
    иначе первая тренировка окна всегда показывала бы ``None``. Упражнения
    без подходов в окне в сводку не попадают вовсе: в них и нет смысла,
    если ими давно не занимались.

    ``order``:

    ``best_rm``
        По лучшему 1RM - так видно, что вообще получается.
    ``last_rm``
        По последнему 1RM - так видно, где сейчас форма.
    ``last_delta``
        По изменению 1RM на последней тренировке.
    ``exercise``
        По алфавиту.

    ``with_rm_only`` убирает упражнения, где 1RM не считается вовсе
    (своё тело без веса, работа на время): их объём виден, но сравнивать
    их 1RM бессмысленно.
    """
    if order not in ("best_rm", "last_rm", "last_delta", "exercise", "sets"):
        raise ValueError(f"неизвестный ключ сортировки: {order!r}")

    if since:
        totals_sql = _TOTALS_SQL.format(since="AND date >= ?")
        details_sql = _BEST_LAST_SQL.format(since="WHERE date >= ?")
        params: tuple[str, ...] = (since, since)
    else:
        totals_sql = _TOTALS_SQL.format(since="")
        details_sql = _BEST_LAST_SQL.format(since="")
        params = ()

    totals = {row["exercise"]: row for row in conn.execute(totals_sql, params[:1])}
    details = {
        row["exercise"]: row
        for row in conn.execute(details_sql, params[1:])
    }

    out: list[ExerciseStats] = []
    for name, row in totals.items():
        if exercise is not None and name != exercise:
            continue
        if group is not None and row["exercise_group"] != group:
            continue
        if min_sets and row["sets"] < min_sets:
            continue

        detail = details.get(name)
        best_rm = detail["best_rm"] if detail else None
        if with_rm_only and best_rm is None:
            continue

        out.append(
            ExerciseStats(
                exercise=name,
                exercise_group=row["exercise_group"],
                equipment=row["equipment"],
                sessions=row["sessions"],
                sets=row["sets"],
                reps_total=row["reps_total"],
                volume=row["volume"],
                first_day=row["first_day"],
                last_day=row["last_day"],
                sources=row["sources"],
                timed_sets=row["timed_sets"],
                koi_total=row["koi_total"],
                best_day=detail["best_day"] if detail else None,
                best_rm=best_rm,
                best_weight=detail["best_weight"] if detail else None,
                best_reps=detail["best_reps"] if detail else None,
                last_day_rm=detail["last_day_rm"] if detail else None,
                last_rm=detail["last_rm"] if detail else None,
                last_weight=detail["last_weight"] if detail else None,
                last_reps=detail["last_reps"] if detail else None,
                last_delta=detail["last_delta"] if detail else None,
                ma_30d=detail["ma_30d"] if detail else None,
                record_rm=detail["record_rm"] if detail else None,
                prev_day=detail["prev_day"] if detail else None,
                prev_rm=detail["prev_rm"] if detail else None,
            )
        )

    keys = {
        "best_rm": lambda s: (s.best_rm is not None, s.best_rm or 0.0),
        "last_rm": lambda s: (s.last_rm is not None, s.last_rm or 0.0),
        "last_delta": lambda s: (
            s.last_delta is not None,
            s.last_delta or 0.0,
        ),
        "exercise": lambda s: s.exercise.casefold(),
        "sets": lambda s: s.sets,
    }[order]

    if order == "exercise":
        out.sort(key=keys)
    else:
        out.sort(key=keys, reverse=True)
    return out[:limit] if limit else out


def since_for_months(conn: sqlite3.Connection, months: int) -> str | None:
    """Дата начала окна «последние N месяцев» относительно последней записи.

    Отсчёт идёт от последней тренировки в базе, а не от сегодняшнего дня:
    журнал может отставать от календаря, и окно, привязанное к «сейчас»,
    пустело бы просто из-за паузы. ``None``, если в базе нет дат или
    окно не имеет смысла (``months <= 0``).
    """
    if months <= 0:
        return None
    row = conn.execute(
        "SELECT date(MAX(date), '-' || ? || ' months') FROM sets"
        " WHERE date IS NOT NULL",
        (str(int(months)),),
    ).fetchone()
    return row[0] if row and row[0] else None


# --------------------------------------------------------------------------
# Список упражнений за последний месяц (UX ручного ввода, 4.12)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecentExercise:
    """Имя, число подходов и последняя дата в окне последних дней."""

    exercise: str
    sets: int
    last_day: str


def recent_exercises(
    conn: sqlite3.Connection, days: int = 28
) -> list[RecentExercise]:
    """Имена, которые реально использовались за последние ``days`` дней.

    Окно считается от **последней даты в базе**, а не от календарного
    «сегодня» (как :func:`since_for_months`): журнал может отставать.
    Входят все имена, включая кардио и «Разминку»: это защита от опечаток
    перед ручным вводом, а не витрина нагрузки. Сортировка - последняя
    дата, затем число подходов, затем имя.
    """
    if days <= 0:
        return []
    row = conn.execute(
        "SELECT MAX(date), date(MAX(date), '-' || ? || ' days') FROM sets"
        " WHERE date IS NOT NULL",
        (str(int(days) - 1),),
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return []
    rows = conn.execute(
        """
        SELECT exercise, COUNT(*) AS sets, MAX(date) AS last_day
          FROM sets
         WHERE date IS NOT NULL AND date >= ?
         GROUP BY exercise
         ORDER BY last_day DESC, sets DESC, exercise
        """,
        (row[1],),
    ).fetchall()
    return [RecentExercise(r[0], r[1], r[2]) for r in rows]


def format_recent_exercises(
    rows: Sequence[RecentExercise], width: int = 36
) -> str:
    """Таблица «имя | подходов | последняя дата» для ноутбука и CLI.

    Ширина адаптивная: ``width`` — нижняя граница, но длинное имя
    расширяет колонку, а не обрезается. Полное «Тяга гантели в наклоне
    одной рукой» должно быть видно целиком — ради этого список и нужен.
    """
    if not rows:
        return "за последний месяц записей нет\n"
    width = max(width, max(len(row.exercise) for row in rows))
    header = f"{'упражнение':<{width}}  {'подходов':>8}  последняя дата"
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.exercise[:width]:<{width}}  {row.sets:>8}  {row.last_day}"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Недельный КОИ
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeeklyKoi:
    """Сумма КОИ за неделю по одному упражнению."""

    week: str
    exercise: str
    exercise_group: str
    sets_done: int
    koi_week: float
    band: str


def weekly_koi(
    conn: sqlite3.Connection,
    *,
    exercise: str | None = None,
    group: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[WeeklyKoi]:
    """Недельные суммы КОИ - по каждому упражнению, из ``v_weekly_koi``.

    КОИ считается для упражнения, а не для группы: у каждого упражнения своя
    база 1RM, поэтому складывать жим и тягу в одну сумму нельзя. Пороги полос
    - из ``core.KOI_BANDS_WEEK``: < 2 лёгкая неделя, 2…3 тяжёлая, 3…4 очень
    тяжёлая, больше 4 «Вы сошли с ума??».

    ``exercise`` фильтрует по названию упражнения, ``group`` - по группе.

    ``limit`` — количество **целых недель**, а не строк. Если 0 или None —
    показать все недели. Это избавляет от обрезки недели посередине и делает
    вывод предсказуемым.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if exercise is not None:
        clauses.append("exercise = ?")
        params.append(exercise)
    if group is not None:
        clauses.append("exercise_group = ?")
        params.append(group)
    if since is not None:
        clauses.append("week >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    limit_n = None
    if limit is not None and int(limit) > 0:
        limit_n = int(limit)

    if limit_n is not None:
        sql = (
            "WITH w AS (SELECT DISTINCT week FROM v_weekly_koi" + where +
            f" ORDER BY week DESC LIMIT {limit_n}) "
            "SELECT week, exercise, exercise_group, sets_done, koi_week, band "
            "FROM v_weekly_koi WHERE week IN (SELECT week FROM w)"
        )
        # params повторно не нужны — фильтры учтены в CTE? Да, CTE использует
        # те же where и те же params? Но в нашем случае, если есть фильтры,
        # where в CTE верный. А во внешнем SELECT фильтруем только по неделям.
        # Но если фильтры есть (exercise/group/since), то правильно брать недели,
        # где есть такие строки. Наш CTE так и делает — ок.
        # Но при передаче в execute нужно передать params один раз.
        exec_params = list(params)
    else:
        sql = (
            "SELECT week, exercise, exercise_group, sets_done, koi_week, band "
            "FROM v_weekly_koi" + where
        )
        exec_params = params

    sql += " ORDER BY week DESC, koi_week DESC"

    out = [
        WeeklyKoi(
            week=row["week"],
            exercise=row["exercise"],
            exercise_group=row["exercise_group"],
            sets_done=row["sets_done"],
            koi_week=row["koi_week"],
            band=row["band"],
        )
        for row in conn.execute(sql, exec_params)
    ]
    return out


def format_weekly_koi(rows: Sequence[WeeklyKoi]) -> str:
    """Таблица недельных сумм КОИ - с шапкой, общей для CLI и ноутбука.

    Одна строка - пара «неделя, упражнение». Колонка «КОИ» - **сумма**
    подходов недели по этому упражнению, «режим» - полоса по этой сумме
    (``core.KOI_BANDS_WEEK``). Отдельной колонки «на подход» нет: она
    усредняла сумму и теряла смысл полосы, которая задана именно для суммы.
    """
    if not rows:
        return "недель с посчитанным КОИ нет\n"
    header = (
        f"{'неделя':<12}  {'упражнение':<28} {'группа':<16} "
        f"{'подходов':>8} {'КОИ':>8}  режим"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.week:<12}  {row.exercise[:28]:<28} "
            f"{row.exercise_group[:16]:<16} {row.sets_done:>8} "
            f"{row.koi_week:>8.2f}  {row.band}"
        )
    lines.append("")
    lines.append(
        "КОИ считается по каждому упражнению отдельно (у каждого своя база 1RM),"
    )
    lines.append(
        "суммы разных упражнений складывать нельзя. Режим - по недельной сумме:"
    )
    lines.append(
        "меньше 2 - лёгкая, 2…3 - тяжёлая, 3…4 - очень тяжёлая, больше 4 - "
        "«Вы сошли с ума??»."
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class WeeklyKoiGroup:
    """Недельный КОИ **по группе**: простое среднее по её упражнениям.

    ``koi_mean`` - среднее недельных сумм упражнений группы, **не взвешенное**
    объёмом или числом подходов: КОИ - безразмерная интенсивность, и вес
    подхода не должен возвращаться в неё через среднее. ``koi_max`` - самое
    тяжёлое упражнение группы за неделю (среднее может скрыть одну тяжёлую
    сессию). ``band`` - полоса по ``koi_mean`` из ``core.KOI_BANDS_WEEK``.
    """

    week: str
    exercise_group: str
    exercises: int
    sets_done: int
    koi_mean: float
    koi_max: float
    band: str


def weekly_koi_by_group(
    conn: sqlite3.Connection,
    *,
    group: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[WeeklyKoiGroup]:
    """Недельный КОИ по группе - **простое среднее** по упражнениям группы.

    Одна строка - пара «неделя, группа». Агрегат считается из
    ``v_weekly_koi`` (по упражнению), а не из ``sets``: КОИ группы - это
    витрина интенсивности, а не новая метрика. Среднее **не взвешивается**
    ни подходами, ни тоннажом: иначе объём вернулся бы внутрь интенсивности
    (та же причина, по которой отвергнута сумма, см. ``weekly_koi``).
    Объём группы живёт отдельно, в ``health/groups.py``.

    Группы без базы 1RM (``taxonomy.NO_RM_GROUPS``: ПРЕСС, ПОДЪЁМ НОГ,
    ПЛАНКА, КАРДИО) в ``v_weekly_koi`` отсутствуют структурно - их нет и
    здесь; витрина пишет это подписью, а не пустыми строками.

    ``group`` фильтрует по группе, ``limit`` - количество **целых недель**
    (как в ``weekly_koi``, а не строк).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if group is not None:
        clauses.append("exercise_group = ?")
        params.append(group)
    if since is not None:
        clauses.append("week >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    limit_n = None
    if limit is not None and int(limit) > 0:
        limit_n = int(limit)

    columns = (
        "week, exercise_group, COUNT(*) AS exercises, "
        "SUM(sets_done) AS sets_done, AVG(koi_week) AS koi_mean, "
        "MAX(koi_week) AS koi_max"
    )
    if limit_n is not None:
        # Фильтры применяются и к выбору недель, и к самим строкам: иначе
        # `--group` с `--limit` вернул бы чужие группы тех же недель.
        outer = (" AND " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "WITH w AS (SELECT DISTINCT week FROM v_weekly_koi" + where +
            f" ORDER BY week DESC LIMIT {limit_n}) "
            f"SELECT {columns} FROM v_weekly_koi"
            " WHERE week IN (SELECT week FROM w)" + outer +
            " GROUP BY week, exercise_group"
        )
        exec_params = params + params
    else:
        sql = (
            f"SELECT {columns} FROM v_weekly_koi" + where +
            " GROUP BY week, exercise_group"
        )
        exec_params = params

    sql += " ORDER BY week DESC, koi_mean DESC, exercise_group"

    return [
        WeeklyKoiGroup(
            week=row["week"],
            exercise_group=row["exercise_group"],
            exercises=row["exercises"],
            sets_done=row["sets_done"],
            koi_mean=row["koi_mean"],
            koi_max=row["koi_max"],
            band=koi_band(row["koi_mean"], KOI_BANDS_WEEK, above=KOI_ABOVE_WEEK),
        )
        for row in conn.execute(sql, exec_params)
    ]


def format_weekly_koi_by_group(rows: Sequence[WeeklyKoiGroup]) -> str:
    """Таблица недельного КОИ по группам - с шапкой и подписью отсутствия.

    Режим стоит рядом со **средним**, а не с максимумом: полоса задана для
    той величины, которую видно в колонке «КОИ ср» (``koi_max`` - справка).
    Подпись прямо говорит, что среднее простое и что объём группы - не здесь.
    """
    if not rows:
        return "недель с посчитанным КОИ нет\n"
    header = (
        f"{'неделя':<12}  {'группа':<22} {'упражнений':>10} "
        f"{'подходов':>8} {'КОИ ср':>8} {'КОИ макс':>9}  режим"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.week:<12}  {row.exercise_group[:22]:<22} "
            f"{row.exercises:>10} {row.sets_done:>8} "
            f"{row.koi_mean:>8.2f} {row.koi_max:>9.2f}  {row.band}"
        )
    lines.append("")
    lines.append(
        "КОИ группы - простое среднее по упражнениям группы за неделю,"
    )
    lines.append(
        "не взвешенное объёмом или числом подходов: КОИ - интенсивность,"
    )
    lines.append(
        "а объём группы виден в тоннаже (health/groups.py)."
    )
    lines.append(
        "Режим - по среднему, полосы те же, что у упражнения: меньше 2 -"
    )
    lines.append(
        "лёгкая, 2…3 - тяжёлая, 3…4 - очень тяжёлая, больше 4 - "
        "«Вы сошли с ума??»."
    )
    lines.append(
        "Групп 16 из 20: ПРЕСС, ПОДЪЁМ НОГ, ПЛАНКА и КАРДИО в КОИ не входят -"
    )
    lines.append(
        "у них не считается 1RM, поэтому базы для КОИ нет."
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# История упражнения за интервал
# --------------------------------------------------------------------------


#: История упражнения: одна строка - одна тренировка.
#:
#: ``since``/``until`` - границы включительно, ISO ``ГГГГ-ММ-ДД`` или ``None``
#: (без границы). Фильтр по ``date``, а не по времени суток: сам интервал
#: задаётся целиком днями.
_HISTORY_SQL = """
WITH f AS (
    SELECT date,
           session_id,
           weight,
           reps,
           rm,
           rm_session,
           rm_delta,
           koi,
           ROW_NUMBER() OVER (
               PARTITION BY date, session_id
               ORDER BY rm DESC, reps DESC, set_index ASC
           ) AS rn
      FROM sets
     WHERE exercise = ?
       AND skipped = 0
       AND is_timed = 0
       AND is_outlier = 0
       {since}
       {until}
)
SELECT date,
       session_id,
       COUNT(*)                 AS sets,
       COALESCE(SUM(reps), 0)   AS reps_total,
       COALESCE(SUM(weight * reps), 0.0) AS volume,
       COALESCE(SUM(koi), 0.0)  AS koi_sum,
       MAX(rm_session)          AS rm_session,
       MAX(rm_delta)            AS rm_delta,
       MAX(CASE WHEN rn = 1 THEN weight END) AS top_weight,
       MAX(CASE WHEN rn = 1 THEN reps   END) AS top_reps,
       MAX(CASE WHEN rn = 1 THEN rm     END) AS top_rm
  FROM f
 GROUP BY date, session_id
 ORDER BY date, session_id
"""


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """Одна тренировка упражнения: верхний подход, 1RM и КОИ за тренировку."""

    date: str
    session_id: str
    sets: int
    reps_total: int
    volume: float
    koi_sum: float
    rm_session: float | None = None
    rm_delta: float | None = None
    top_weight: float | None = None
    top_reps: int | None = None
    top_rm: float | None = None

    @property
    def top_set(self) -> str:
        if self.top_weight is None:
            return "—"
        return f"{self.top_weight:g} кг x {self.top_reps}"

    @property
    def band(self) -> str | None:
        return koi_band(self.koi_sum)


def history(
    conn: sqlite3.Connection,
    exercise: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    reverse: bool = False,
) -> list[HistoryRow]:
    """История одной тренировки упражнения за интервал ``since``…``until``.

    Интервал задаётся в ISO (``ГГГГ-ММ-ДД``) и **включает обе границы**:
    ``since`` - с какого дня показывать, ``until`` - по какой. Любая граница
    может быть ``None``, тогда ограничения с этой стороны нет. Пусто с обеих
    сторон - вся история упражнения.

    ``limit`` обрезает вывод **после** сортировки по дате; какие именно
    тренировки оставить, зависит от ``reverse``:

    * ``reverse=False`` (по умолчанию) - порядок от первой даты к последней,
      ``limit`` оставляет первые (самые старые) ``limit`` тренировок;
    * ``reverse=True`` - порядок от последней даты к первой, ``limit``
      оставляет последние (самые свежие) ``limit`` тренировок.

    ``None`` вместо ``limit`` - все тренировки интервала.
    """
    sql = _HISTORY_SQL.format(
        since="AND date >= ?" if since else "",
        until="AND date <= ?" if until else "",
    )
    params: list[Any] = [exercise]
    if since:
        params.append(since)
    if until:
        params.append(until)

    rows = [
        HistoryRow(
            date=row["date"],
            session_id=row["session_id"],
            sets=row["sets"],
            reps_total=row["reps_total"],
            volume=row["volume"],
            koi_sum=row["koi_sum"],
            rm_session=row["rm_session"],
            rm_delta=row["rm_delta"],
            top_weight=row["top_weight"],
            top_reps=row["top_reps"],
            top_rm=row["top_rm"],
        )
        for row in conn.execute(sql, params)
    ]
    if reverse:
        rows.reverse()
    if limit:
        rows = rows[: int(limit)]
    return rows


def format_history(
    rows: Sequence[HistoryRow],
    exercise: str,
    *,
    since: str | None = None,
    until: str | None = None,
    width: int = 26,
    reverse: bool = False,
) -> str:
    """Таблица истории упражнения с подписью интервала и направления."""
    window = f"{since or 'начало'} … {until or 'конец'}"
    if not rows:
        return (
            f"история «{exercise}» за {window}: пусто\n"
            f"(в этом интервале нет подходов; проверьте название упражнения "
            f"и даты)\n"
        )
    order = "от новых к старым" if reverse else "от старых к новым"
    lines = [f"=== {exercise}: история за {window} ({order}) ==="]
    header = (
        f"{'дата':>10} {'верхний подход':>16} {'подх':>5} {'повторов':>8} "
        f"{'объём':>9} {'1RM':>7} {'Δ 1RM':>7} {'КОИ':>7}  режим"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in rows:
        rm = f"{row.rm_session:.1f}" if row.rm_session is not None else "—"
        delta = f"{row.rm_delta:+.2f}" if row.rm_delta is not None else "—"
        lines.append(
            f"{row.date:>10} {row.top_set:>16} {row.sets:>5} "
            f"{row.reps_total:>8} {row.volume:>9.0f} {rm:>7} {delta:>7} "
            f"{row.koi_sum:>7.2f}  {row.band or '—'}"
        )
    lines.append("")
    lines.append(
        "верхний подход - лучший подход тренировки; 1RM = его 1RM (Brzycki); "
        "Δ 1RM - к предыдущей"
    )
    lines.append(
        "тренировке упражнения; КОИ - сумма подходов тренировки, режим по "
        "core.KOI_BANDS."
    )
    return "\n".join(lines) + "\n"


#: Разбор расчёта по одному подходу: сырое значение -> режим веса ->
#: переписанный вес -> 1RM -> 1RM тренировки -> дельта -> скользящее среднее
#: -> КОИ. Показываются все промежуточные величины, чтобы «111.7» не выглядело
#: магией.
_EXPLAIN_SQL = """
SELECT id, source, date, session_id, set_index,
       exercise, exercise_group, weight_mode,
       raw_weight, added_weight, weight, reps, time_seconds,
       CASE WHEN weight_mode <> 'absolute'
                 AND weight IS NOT NULL AND added_weight IS NOT NULL
            THEN weight - added_weight END AS body_weight,
       is_warmup, skipped, note, is_timed, timed_reason,
       is_outlier, outlier_rule,
       rm, rm_session, rm_delta, rm_ma_30d, koi
  FROM sets
 WHERE exercise = ?
 ORDER BY date DESC, session_id DESC, set_index DESC
 LIMIT ?
"""


def explain_sets(
    conn: sqlite3.Connection,
    exercise: str,
    *,
    limit: int = 5,
) -> list[sqlite3.Row]:
    """Последние ``limit`` подходов упражнения со всеми промежуточными полями.

    Возвращает строки как есть: что именно печатать - дело ``koilog explain``.
    Каждая строка несёт и ``body_weight`` (``weight - added_weight`` для
    режимов с телом, ``NULL`` для ``absolute``) - из неё видно, как из
    собственного веса и добавки получился суммарный ``weight``.
    """
    return list(conn.execute(_EXPLAIN_SQL, (exercise, max(1, int(limit)))))


def _cell(value: float | None, digits: int = 1) -> str:
    """Число для печати; ``None`` - пустая ячейка (вызывающий ставит «—»)."""
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def format_explain(rows: Sequence[sqlite3.Row]) -> str:
    """Таблица «последние подходы» для ``explain`` - общая для CLI и ноутбука.

    Колонки: ``сырой`` - что записано в файле; ``режим`` - как трактуется
    (taxonomy); ``разм`` - разминка (в 1RM/КОИ не входит); ``свой`` - вес
    собственного тела, взятый для подхода; ``вес`` - суммарный рабочий;
    ``+`` - добавка к телу (жилет); ``1RMтрен`` - лучший 1RM тренировки;
    ``Δ`` - к прошлой тренировке; ``МА30`` - скользящее среднее (база КОИ);
    ``полоса`` - по ``core.KOI_BANDS``. Флаги (пропуск, время, выброс)
    дописываются в конец строки.
    """
    if not rows:
        return "подходов нет\n"
    header = (
        f"{'дата':>10} {'п':>2} {'разм':>4} {'сырой':>7} {'режим':<16} "
        f"{'свой':>6} {'вес':>7} {'+':>6} {'повт':>4} {'1RM':>7} "
        f"{'1RMтрен':>8} {'Δ':>7} {'МА30':>7} {'КОИ':>7}  полоса"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        flags = []
        if row["skipped"]:
            flags.append("пропущен")
        if row["is_timed"]:
            flags.append("время")
        if row["is_outlier"]:
            flags.append(f"выброс:{row['outlier_rule']}")
        suffix = ("  [" + ", ".join(flags) + "]") if flags else ""
        warm = "разм" if row["is_warmup"] else "—"
        lines.append(
            f"{row['date']:>10} "
            f"{'' if row['set_index'] is None else row['set_index']:>2} "
            f"{warm:>4} "
            f"{_cell(row['raw_weight']) or '—':>7} "
            f"{(row['weight_mode'] or '—'):<16} "
            f"{_cell(row['body_weight']) or '—':>6} "
            f"{_cell(row['weight']) or '—':>7} "
            f"{_cell(row['added_weight']) or '—':>6} "
            f"{'' if row['reps'] is None else row['reps']:>4} "
            f"{_cell(row['rm']) or '—':>7} "
            f"{_cell(row['rm_session']) or '—':>8} "
            f"{_cell(row['rm_delta'], 2) or '—':>7} "
            f"{_cell(row['rm_ma_30d']) or '—':>7} "
            f"{_cell(row['koi'], 3) or '—':>7}  "
            f"{koi_band(row['koi']) or '—'}{suffix}"
        )
    lines.append("")
    lines.append(
        "разм - разминка, в 1RM/КОИ не входит; сырой - что записано в файле; "
        "режим -"
    )
    lines.append(
        "как трактуется (taxonomy); свой - вес тела для подхода, вес - "
        "суммарный рабочий,"
    )
    lines.append(
        "+ - добавка (жилет); 1RMтрен - лучший 1RM тренировки; Δ - к прошлой "
        "тренировке;"
    )
    lines.append(
        "МА30 - среднее 1RM за 30 дней по упражнению (база КОИ);"
    )
    lines.append(
        "КОИ (коэффициент относительной интенсивности) = "
        "повторы / (1 − вес/МА30) / 100; полоса - по core.KOI_BANDS."
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Периоды и графики
# --------------------------------------------------------------------------

#: Длины периодов в днях: месяц, квартал, полгода, год. Скользящие окна назад
#: от последней тренировки по упражнению, а не календарные куски: иначе в
#: графике появляются пустые половины ��ода, в которых ничего не делалось, и
#: объём за месяц сравнивается с объёмом за две недели.
PERIOD_DAYS: tuple[int, ...] = (30, 90, 180, 365)

PERIOD_NAMES: Mapping[int, str] = {
    30: "месяц",
    90: "квартал",
    180: "полгода",
    365: "год",
}


@dataclass(frozen=True, slots=True)
class PeriodStats:
    """Одно скользящее окно по упражнению.

    ``delta`` - насколько вырос лучший 1RM окна относительно лучшего 1RM
    предыдущего окна той же длины. Это и есть тот самый «1RM-дельта», который
    в недельной сводке считался как разница к предыдущей тренировке и из-за
    этого скакал то вверх, то вниз на каждом разовом подходе.
    """

    days: int
    since: str
    until: str
    sessions: int
    sets: int
    reps_total: int
    volume: float
    best_rm: float | None
    prev_rm: float | None
    koi_sum: float

    @property
    def name(self) -> str:
        return PERIOD_NAMES.get(self.days, f"{self.days} дн.")

    @property
    def delta(self) -> float | None:
        if self.best_rm is None or self.prev_rm is None:
            return None
        return self.best_rm - self.prev_rm

    @property
    def koi_average(self) -> float:
        return self.koi_sum / self.sets if self.sets else 0.0

    @property
    def avg_reps(self) -> float:
        return self.reps_total / self.sets if self.sets else 0.0

    @property
    def empty(self) -> bool:
        return not self.sets


_PERIOD_SQL = """
SELECT COUNT(DISTINCT session_id)          AS sessions,
       COUNT(*)                            AS sets,
       COALESCE(SUM(reps), 0)              AS reps_total,
       COALESCE(SUM(weight * reps), 0.0)   AS volume,
       MAX(rm)                             AS best_rm,
       COALESCE(SUM(koi), 0.0)             AS koi_sum
  FROM sets
 WHERE exercise = ?
   AND date BETWEEN ? AND ?
   AND skipped = 0
   AND is_timed = 0
   AND is_outlier = 0
"""


def _shift(day: str, days: int) -> str:
    """Дата на ``days`` дней раньше, в формате ``ГГГГ-ММ-ДД``."""
    year, month, dom = (int(part) for part in day.split("-"))
    index = year * 372 + (month - 1) * 31 + dom - 1 - days
    # 372 = 12*31: месяцы тут как дни, это сдвиг для оценки, а не календарь.
    year, rest = divmod(index, 372)
    month, dom = divmod(rest, 31)
    return f"{year:04d}-{month + 1:02d}-{dom + 1:02d}"


def periods(
    conn: sqlite3.Connection,
    exercise: str,
    *,
    days: Sequence[int] = PERIOD_DAYS,
    until: str | None = None,
) -> list[PeriodStats]:
    """Скользящие окна по упражнению: объём, лучший 1RM и изменение 1RM.

    Окна отсчитываются назад от последней тренировки по этому упражнению
    (``until`` можно задать явно - так же считает недельная сводка от
    сегодняшнего дня). Для каждого окна дополнительно читается предыдущее
    окно той же длины, чтобы получить дельту.
    """
    if until is None:
        row = conn.execute(
            "SELECT MAX(date) FROM sets WHERE exercise = ?", (exercise,)
        ).fetchone()
        until = row[0] if row and row[0] else None
    if until is None:
        return []

    out: list[PeriodStats] = []
    for window in days:
        since = _shift(until, window)
        prev_since = _shift(until, window * 2)

        def read(start: str, end: str) -> sqlite3.Row:
            return conn.execute(
                _PERIOD_SQL, (exercise, start, end)
            ).fetchone()

        current = read(since, until)
        previous = read(prev_since, _shift(since, 1))
        out.append(
            PeriodStats(
                days=window,
                since=since,
                until=until,
                sessions=current["sessions"] or 0,
                sets=current["sets"] or 0,
                reps_total=current["reps_total"] or 0,
                volume=current["volume"] or 0.0,
                best_rm=current["best_rm"],
                prev_rm=previous["best_rm"],
                koi_sum=current["koi_sum"] or 0.0,
            )
        )
    return out


def ascii_bar(value: float, peak: float, *, width: int = 30, mark: str = "#") -> str:
    """Полоска из ``#`` заданной ширины - граф без библиотек.

    Пустые окна рисуются пробелом целиком, иначе нулевой объём выглядел бы
    как «график есть, а нагрузки нет». Дробная часть отбрасывается: при
    ``width = 30`` один знак - это уже треть объёма месяца, и попытка показать
    точнее только добавляет шума.
    """
    if peak <= 0:
        return " " * width
    filled = int(round(width * max(value, 0.0) / peak))
    return mark * max(0, min(width, filled))


def format_periods(rows: Sequence[PeriodStats]) -> str:
    """Периоды + два графика: изменение 1RM и объём.

    Графики - ASCII, а не картинки: проект держится на стандартной
    библиотеке, а matplotlib в зависимости не числится. Числа всё равно
    доступны точно - ``koilog plan --csv`` печатает их в соседних колонках.
    """
    if not rows:
        return "нет периодов: упражнение не найдено в базе\n"

    header = (
        f"{'период':>8} {'окно':>25} {'трен':>5} {'подх':>5} {'объём':>9} "
        f"{'1RM':>7} {'дельта':>8} {'КОИ/подх':>9}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        delta = "—" if row.delta is None else f"{row.delta:+.2f}"
        best = "—" if row.best_rm is None else f"{row.best_rm:.1f}"
        lines.append(
            f"{row.name:>8} {row.since + ' … ' + row.until:>25} "
            f"{row.sessions:>5} {row.sets:>5} {row.volume:>9.0f} "
            f"{best:>7} {delta:>8} {row.koi_average:>9.2f}"
        )

    lines.append("")
    lines.append("изменение 1RM к предыдущему периоду:")
    lines.append(_chart(rows, lambda r: abs(r.delta or 0.0), signed=True))

    lines.append("")
    lines.append("объём (тонна·повторов) за период:")
    lines.append(_chart(rows, lambda r: r.volume))

    return "\n".join(lines) + "\n"


def _chart(rows: Sequence[PeriodStats], value, *, signed: bool = False) -> str:
    """Полосы по периодам с общей шкалой."""
    values = [value(r) for r in rows]
    peak = max(values) if values else 0.0
    out: list[str] = []
    for row, item in zip(rows, values):
        if signed:
            sign = "+" if (row.delta or 0) > 0 else ("-" if (row.delta or 0) < 0 else " ")
            width = 15
            filled = int(round(width * item / peak)) if peak else 0
            bar = (" " * (width - filled) + "#" * filled) if (row.delta or 0) >= 0 \
                else ("#" * filled + " " * (width - filled))
            out.append(f"{row.name:>8} {sign}{item:6.2f} |{bar}|")
        else:
            bar = ascii_bar(item, peak)
            amount = "—" if row.empty else f"{item:.0f}"
            out.append(f"{row.name:>8} {amount:>9} |{bar}|")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Программа из базы
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgramPlan:
    """Программа вместе с данными, из которых она выросла.

    Держит и статистику, и расчёт: по одному упражнению сразу видно, от
    чего отталкивались и куда пришли.

    ``hands`` - на сколько рук делится вес снаряда. Это справочное поле для
    отчёта, а не множитель в расчёте: лестница и записанный вес в одних
    единицах, поэтому программа строится с ``hands = 1``.
    """

    stats: ExerciseStats
    available: tuple[float, ...]
    hands: int
    program: Progression
    wave: TransitionPlan
    reference_rm: float | None
    reason: str | None
    warnings: tuple[str, ...] = ()
    profile: str = ""

    @property
    def built(self) -> bool:
        """Есть ли что показать.

        ``reason`` означает две разные вещи: либо данных нет и строить
        нечего (упражнение не найдено, 1RM не считается), либо программа
        построена, но упёрлась в край списка весов. Второе - не ошибка, а
        нормальное завершение, и по нему нужен нулевой код возврата.
        """
        return len(self.program) > 0

    def __iter__(self):
        return iter(self.program)


def available_weights(
    conn: sqlite3.Connection | None,
    exercise: str,
    *,
    min_weight: float = 0.0,
    vest: bool = False,
    body_weight: float | None = None,
) -> tuple[float, ...]:
    """Веса, которые можно поставить в это упражнение, по возрастанию.

    Список берётся из инвентаря (:mod:`koilog.equipment`), а не из истории, и
    это главное изменение: программа не имеет права предложить вес, которого
    в зале нет. Упражнение с двумя гантелями отдаёт суммарные килограммы
    (у ``Жим гантелей лежа`` это 10…80 вместо 5…40 на пару), поэтому
    ``hands`` у такого профиля равен 2 и ``core.next_weight`` делит вес перед
    поиском, поэтому список снарядов не расходится с реальностью.

    Единицы лестницы и записанного веса совпадают: для гантелей в две руки
    это суммарные килограммы. Поэтому ``core.next_weight`` вызывается с
    ``hands = 1`` - делить вес не нужно, а вдвое уменьшенный поиск в лестнице
    на гантели просто ничего не находит.

    ``conn`` оставлен параметром и не используется: раньше список выводился
    запросом по таблице ``sets``, и его подпись нужна была вызывающим
    (``tests``). Убрать параметр - значит сломать вызовы ради единственного
    аргумента, который больше не нужен; при этом новый модуль к БД не ходит.

    Пустой кортеж - ответ «внешнего веса тут нет»: для планки, подъёма ног
    и кардио так и должно быть, и программа по ним не строится.
    """
    return equipment.available_weights(
        exercise,
        min_weight=min_weight,
        vest=vest,
        body_weight=body_weight,
    )


def _infer_hands(exercise: str, *, vest: bool = False) -> int:
    """На сколько рук делится вес в этом упражнении - свойство снаряда.

    Это поле для отчёта, а не множитель в арифметике: инвентарь отдаёт
    лестницу в тех же единицах, что и записанный вес. Раньше здесь стояло 2
    для всего, где в названии группы есть «гантель», потом Always 1, потому
    что список выводился из базы уже в суммарных килограммах, - и оба
    варианта были неверными по очереди. Теперь ответ берётся из
    :mod:`koilog.equipment`, где он задан руками вместе с лестницей.
    """
    return equipment.hands_of(exercise, vest=vest)


def progression_from_db(
    conn: sqlite3.Connection,
    exercise: str,
    *,
    available: Sequence[float] | None = None,
    hands: int | None = None,
    sets: int = 3,
    steps: int = 18,
    warmup_weight: float | None = None,
    warmup_reps: int = 0,
    use_db_reference: bool = False,
    count: int = TRANSITIONS,
    start_weight: float | None = None,
    start_reps: int | None = None,
    vest: bool = False,
    body_weight: float | None = None,
    optimal_reps: bool = False,
) -> ProgramPlan:
    """Построить программу роста нагрузки от последней тренировки.

    Стартовые вес и повторы берутся из ``ExerciseStats.last_*`` - то есть
    из лучшего подхода последней тренировки по этому упражнению, а не
    выставляются руками.

    Список весов - инвентарь из :mod:`koilog.equipment`, а не история тренировок.
    Записанный вес при этом может не лежать на лестнице (82 кг у
    ``Отжиманий``, 44 кг у ``Тяги штанги в наклоне``), поэтому он
    приводится к ближайшему снаряду не тяжелее (``core.snap_to_ladder``),
    а расхождение попадает в ``warnings``. Молча начинать с чужого веса
    нельзя, а отказываться строить программу из-за половины килограмма -
    тоже нельзя.

    ``use_db_reference`` переключает базу КОИ со «собственный 1RM стартового
    подхода» на скользящее среднее за 30 дней из базы.
    По умолчанию выключено, чтобы расчёт оставался сопоставимым с прежним.

    Если по упражнению не нашлось ни одного 1RM (своё тело без веса,
    работа на время), возвращается план с пустой программой и
    пояснением в ``reason`` - исключение тут неуместно, это не ошибка
    данных, а отсутствие данных.
    """
    stats = _one_stats(conn, exercise)
    if stats is None:
        return ProgramPlan(
            stats=_empty_stats(exercise),
            available=(),
            hands=_infer_hands(exercise, vest=vest),
            program=progression((), 1.0, 1, sets=sets, steps=0),
            wave=transitions((), 1.0, 1),
            reference_rm=None,
            reason=f"упражнение «{exercise}» не найдено в базе",
        )

    # ``hands`` - справочное поле отчёта («вес делится на две руки»), а
    # множитель в поиске следующего снаряда. Лестница инвентаря уже в тех же
    # единицах, что и записанный вес: ``Жим гантелей лежа`` - это 40 кг
    # суммарно, и в списке 40 тоже есть. Делить вес на 2 перед поиском
    # значит искать 20 в списке, где его может не быть, и получать обрыв
    # программы на первом же шаге. Явный ``hands=`` из вызова уважается,
    # потому что он означает «я передал список в пересчёте на одну руку».
    calc_hands = hands if hands is not None else 1
    reported_hands = hands if hands is not None else _infer_hands(exercise, vest=vest)
    weights = (
        tuple(float(w) for w in available)
        if available is not None
        else available_weights(
            conn, exercise, vest=vest, body_weight=body_weight
        )
    )

    weight = start_weight if start_weight is not None else stats.last_weight
    reps = start_reps if start_reps is not None else stats.last_reps

    if weight is None or reps is None:
        return ProgramPlan(
            stats=stats,
            available=weights,
            hands=reported_hands,
            program=progression(weights, 1.0, 1, sets=sets, steps=0),
            wave=transitions(weights, 1.0, 1),
            reference_rm=None,
            reason=(
                f"у «{exercise}» нет считаемого 1RM "
                f"({stats.sets} подходов, из них на время: {stats.timed_sets}); "
                f"программу строить не от чего"
            ),
        )

    if not weights:
        profile = equipment.profile_of(exercise, vest=vest)
        return ProgramPlan(
            stats=stats,
            available=(),
            hands=reported_hands,
            program=progression(weights, weight, reps, sets=sets, steps=0),
            wave=transitions(weights, weight, reps),
            reference_rm=stats.last_rm,
            reason=(
                f"для «{exercise}» нет внешнего веса "
                f"({profile.title}); переходов по весам не будет"
                if not profile.weights
                else "инвентарь для упражнения не задан, "
                     "программу строить не от чего"
            ),
        )

    warnings: list[str] = []
    snapped = snap_to_ladder(weight, weights, calc_hands)
    if snapped is None:
        return ProgramPlan(
            stats=stats,
            available=weights,
            hands=reported_hands,
            program=progression(weights, weight, reps, sets=sets, steps=0),
            wave=transitions(weights, weight, reps),
            reference_rm=stats.last_rm,
            reason=(
                f"записанный вес {weight:g} кг легче самого лёгкого снаряда "
                f"({weights[0]:g} кг) - программу не с чего начинать"
            ),
        )
    if abs(snapped - weight) > 1e-9:
        warnings.append(
            f"в инвентаре нет {weight:g} кг: программа стартует с ближайшего "
            f"не тяжелее - {snapped:g} кг"
        )
        weight = snapped

    reference = stats.ma_30d if use_db_reference else None
    program = progression(
        weights,
        weight,
        reps,
        sets=sets,
        hands=calc_hands,
        warmup_weight=warmup_weight,
        warmup_reps=warmup_reps,
        steps=steps,
        reference_rm=reference,
    )
    wave = transitions(
        weights,
        weight,
        reps,
        hands=calc_hands,
        count=count,
        optimal_reps=optimal_reps,
    )

    reason = program.stopped
    if not reason and wave.stopped and wave.stopped != "стартовый вес не найден в списке снарядов":
        reason = wave.stopped

    return ProgramPlan(
        stats=stats,
        available=weights,
        hands=reported_hands,
        program=program,
        wave=wave,
        reference_rm=program.steps[0].reference_rm if program.steps else None,
        reason=reason,
        warnings=tuple(warnings),
        profile=equipment.profile_of(exercise, vest=vest).id,
    )


def _one_stats(conn: sqlite3.Connection, exercise: str) -> ExerciseStats | None:
    found = exercise_report(conn, exercise=exercise)
    return found[0] if found else None


def _empty_stats(exercise: str) -> ExerciseStats:
    group = taxonomy.group_of(exercise)
    return ExerciseStats(
        exercise=exercise,
        exercise_group=group.name,
        equipment=group.equipment,
        sessions=0,
        sets=0,
        reps_total=0,
        volume=0.0,
        first_day="",
        last_day="",
        sources="",
        timed_sets=0,
        koi_total=0.0,
    )


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------


def format_report(rows: Sequence[ExerciseStats], *, width: int = 30) -> str:
    """Таблица статистики по упражнениям для чтения глазами.

    Колонки:

    ``подх``
        Сколько подходов упражнения попало в окно.
    ``лучший``
        Лучший 1RM **внутри окна** (лучший подход лучшей тренировки окна).
    ``рекорд``
        Лучший 1RM за **всю** историю упражнения, даже если рекорд поставлен
        до начала окна. Это точка отсчёта для следующей колонки.
    ``посл.``
        1RM последней тренировки в окне.
    ``Δ к рекорду``
        ``посл. − рекорд``: 0 значит «на рекорде», минус - «отстаём на
        столько-то кг». Не путать с дельтой к предыдущей тренировке: две
        одинаковые тренировки подряд давали там 0 и выглядели как застой.
    ``МА30``
        Скользящее среднее 1RM за последние 30 дней **по этому упражнению**
        (база формулы КОИ). Считается по календарным дням, а не по числу
        тренировок; у упражнений «тело + жилет» включает вес тела.
    ``объём``
        Сумма ``вес x повторы`` по подходам окна, в килограммо-повторах.
    """
    if not rows:
        return "нет упражнений под фильтр\n"

    lines = [
        f"{'упражнение':<{width}} {'группа':<20} {'подх':>5} {'лучший':>7} "
        f"{'рекорд':>7} {'посл.':>7} {'Δ к рекорду':>11} {'МА30':>7} {'объём':>10}"
    ]
    lines.append("-" * (width + 20 + 5 + 4 * 7 + 11 + 10 + 8))
    for row in rows:
        best = f"{row.best_rm:.1f}" if row.best_rm is not None else "—"
        record = f"{row.record_rm:.1f}" if row.record_rm is not None else "—"
        last = f"{row.last_rm:.1f}" if row.last_rm is not None else "—"
        delta = (
            "0.0"
            if row.delta_to_record is not None
            and abs(row.delta_to_record) < 0.05
            else (
                f"{row.delta_to_record:+.1f}"
                if row.delta_to_record is not None
                else "—"
            )
        )
        ma = f"{row.ma_30d:.1f}" if row.ma_30d is not None else "—"
        lines.append(
            f"{row.exercise[: width - 1]:<{width}} "
            f"{row.exercise_group[:19]:<20} "
            f"{row.sets:>5} {best:>7} {record:>7} {last:>7} {delta:>11} {ma:>7} "
            f"{row.volume:>10.0f}"
        )
    return "\n".join(lines) + "\n"


def format_plan(plan: ProgramPlan) -> str:
    """Программа роста нагрузки для чтения глазами.

    Переходы на следующий вес печатает отдельная :func:`format_transitions`:
    в ноутбуке под них отведена своя ячейка, и здесь они дублировались.
    """
    stats = plan.stats
    out: list[str] = []
    out.append(f"=== {stats.exercise}  ({stats.exercise_group}) ===")
    if stats.sets:
        out.append(
            f"история: {stats.sets} подходов, {stats.sessions} тренировок, "
            f"{stats.first_day} ... {stats.last_day}"
        )
        out.append(
            f"лучший 1RM: {stats.best_set} ({stats.best_day})"
        )
        if stats.record_rm is not None:
            out.append(f"рекорд:     {stats.record_rm:.1f} кг за всю историю")
        out.append(
            f"последний:  {stats.last_set} ({stats.last_day_rm})"
        )
        if stats.last_delta is not None:
            if stats.last_delta > 0:
                word = "рост"
            elif stats.last_delta < 0:
                word = "падение"
            else:
                word = "без изменений"
            prev = stats.prev_day or "предыдущая тренировка"
            prev_rm = (
                f" ({stats.prev_rm:.1f})" if stats.prev_rm is not None else ""
            )
            out.append(
                f"изменение к прошлой тренировке {prev}{prev_rm}: "
                f"{stats.last_delta:+.2f} кг ({word})"
            )
        if stats.last_rm is not None and stats.delta_to_record is not None:
            gap = stats.delta_to_record
            tail = (
                "это и есть рекорд"
                if abs(gap) < 0.05
                else f"отставание от рекорда {gap:+.2f} кг"
            )
            out.append(f"Δ к рекорду: {gap:+.2f} кг ({tail})")
        if stats.ma_30d is not None:
            out.append(
                f"МА30 (средний 1RM за 30 дней по упражнению, база КОИ): "
                f"{stats.ma_30d:.1f}"
            )
    if plan.reason:
        out.append(f"! остановка: {plan.reason}")
    for warning in plan.warnings:
        out.append(f"~ {warning}")
    if plan.profile:
        profile = equipment.PROFILES[plan.profile]
        note = f"  ({profile.note})" if profile.note else ""
        out.append(
            f"инвентарь: {profile.title}, {len(plan.available)} значений, "
            f"вес делится на {plan.hands}{note}"
        )

    if len(plan.program):
        out.append("")
        out.append("программа (база 1RM пересчитывается по ходу):")
        header = (
            f"{'шаг':>4}  {'подходы':<26} {'база':>7} {'нагрузка':>9} "
            f"{'КОИ':>8}  режим"
        )
        out.append(header)
        out.append("-" * len(header))
        for step in plan.program:
            sets = " ".join(f"{s.weight:g}x{s.reps}" for s in step.sets)
            warm = f" + разминка {step.warmup.weight:g}x{step.warmup.reps}" if step.warmup else ""
            load = f"{step.load:.1%}" if step.load is not None else "—"
            out.append(
                f"{step.index:>4}  {sets:<26} {step.reference_rm:>7.1f} "
                f"{load:>9} {step.koi:>8.2f}  {step.band}{warm}"
            )

    return "\n".join(out) + "\n"


def format_transitions(wave: TransitionPlan | Sequence[Any]) -> str:
    """Таблица переходов на следующий вес - отдельно от программы.

    ``format_plan`` переходы больше не печатает: в ноутбуке для них есть
    отдельная ячейка, и та же информация выходила дважды. Общий форматтер
    держит CLI и ноутбук в одном виде.
    """
    if not wave:
        return ""
    lines = [
        "переходы на следующий вес:",
        f"{'из':>14}  ->  {'новый вес':<16} {'1RM':>7} {'дельта':>8}",
        "-" * 52,
    ]
    for step in wave:
        lines.append(
            f"{step.from_weight:g} кг x {step.from_reps:<2}   ->  "
            f"{step.weight:g} кг x {step.reps:<2}      "
            f"{step.rm:>7.2f} {step.delta:>+8.2f}"
        )
    return "\n".join(lines) + "\n"


def progression_from_available(
    available: Sequence[float],
    start_weight: float,
    start_reps: int,
    *,
    hands: int = 1,
    sets: int = 3,
    steps: int = 18,
    warmup_weight: float | None = None,
    warmup_reps: int = 0,
    reference_rm: float | None = None,
    count: int = TRANSITIONS,
) -> tuple[Progression, TransitionPlan]:
    """Программа и переходы по явно заданному списку снарядов.

    Обёртка над :func:`koilog.core.progression` для случая, когда базы под рукой
    нет, а список гантелей известен.
    """
    return (
        progression(
            available,
            start_weight,
            start_reps,
            sets=sets,
            hands=hands,
            warmup_weight=warmup_weight,
            warmup_reps=warmup_reps,
            steps=steps,
            reference_rm=reference_rm,
        ),
        transitions(
            available,
            start_weight,
            start_reps,
            hands=hands,
            count=count,
        ),
    )
