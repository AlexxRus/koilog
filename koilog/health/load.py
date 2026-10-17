"""Внешняя нагрузка из ``sets``: тоннаж, sRPE, ACWR, монотонность, стрейн.

Это ответ на первую половину рамки Impellizzeri et al. (Sports Med, 2019):
**внешняя** нагрузка - то, что спортсмен сделал (тоннаж, подходы, КОИ), в
противоположность **внутренней** (пульс, ВСР, сон - модуль :mod:`koilog.health.apple`).
Болезнь здесь - исход, на который обе нагрузки могли повлиять.

Считаются две вещи:

- **Отдельные тренировки** - тоннаж, интенсивностно-взвешенный тоннаж (тоннаж ×
  средний %1RM), КОИ, sRPE-суррогат (длительность × RPE из часов), лучший e1RM дня.
- **Скользящие окна** - острый/хронический ACWR по sRPE (7/28), его EWMA-версия
  (Williams et al., 2017), монотонность и стрейн (Foster, 1998), недельный тоннаж.

Формулы собраны в чистые функции без базы: их проверяют тесты на числах, а не на
походах в SQLite. Материализация в ``load_daily`` и ``health_days`` - отдельно.

Метрика «нагрузка» для ACWR - **sRPE**, а не тоннаж: так принято в литературе по
ACWR (session-RPE), и у тоннажа своя единица (кг), которую нельзя сравнивать
между упражнениями с разным весом тела. Тоннаж идёт отдельной колонкой.

Про малые числа: ACWR - **описательная** метрика (Impellizzeri et al., 2020,
критикуют пороги), поэтому зоны Gabbett (2016) здесь не приговор, а разметка
для графика. Монотонность > 2 (Foster) - повод посмотреть, а не диагноз.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date as date_cls
from datetime import timedelta

from .. import etl

__all__ = [
    "DEFAULT_WINDOW",
    "LoadDay",
    "acwr",
    "acwr_ewma",
    "ewma",
    "format_summary",
    "health_days",
    "load_daily",
    "monotonicity",
    "series",
    "srpe",
    "strain",
    "summarize",
    "weekly_tonnage",
]

#: То же окно, что у Apple-метрик: раньше полного набора часовых данных нет,
#: и связывать нагрузку с болезнью на половине признаков смысла мало.
DEFAULT_WINDOW = "2024-01-16"

#: Сколько минут «стоит» один подход, если часов на тренировке не было. Нужно
#: только как запасной вариант: с 2024 силовые пишутся часами в ≥97 % дней.
SRPE_MIN_PER_SET = 3.5

#: RPE по умолчанию, когда часы не записали ни явной, ни оценочной оценки
#: усилия (реальный RPE почти не заполнен: 690 из 779 записей - «5»).
SRPE_DEFAULT_RPE = 5.0

#: Порог монотонности Foster: выше - «серая зона» однообразной нагрузки.
MONOTONY_ALERT = 2.0

#: Сколько последних результатов считаем «формой», сколько предыдущих -
#: «базой», и на сколько форма должна упасть, чтобы это был красный флаг.
#: Сила при прежнем объёме не должна падать (Meeusen et al., MSSE, 2013).
RM_REGRESS_SHORT = 7
RM_REGRESS_LONG = 21
RM_REGRESS_MIN = 3
RM_REGRESS_DROP = 0.95


# --------------------------------------------------------------------------
# Чистые метрики
# --------------------------------------------------------------------------


def ewma(values: list[float], span: int) -> list[float]:
    """Экспоненциально-взвешенное среднее, ``λ = 2/(span+1)`` (Williams 2017).

    Первое значение становится начальным состоянием (сезонность «разогрева»
    отсутствует): иначе окно на старте ряда раздувается с нуля.
    """
    if span < 1:
        raise ValueError("span должен быть >= 1")
    alpha = 2.0 / (span + 1.0)
    out: list[float] = []
    prev: float | None = None
    for value in values:
        prev = value if prev is None else alpha * value + (1.0 - alpha) * prev
        out.append(prev)
    return out


def _rolling_mean(values: list[float], window: int) -> list[float]:
    """Скользящее среднее по последним ``window`` значениям включительно."""
    out: list[float] = []
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        out.append(total / min(index + 1, window))
    return out


def acwr(loads: list[float], short: int = 7, long: int = 28) -> list[float | None]:
    """Острый/хронический ACWR: среднее за ``short`` / за ``long``.

    ``None``, если хроническая нагрузка нулевая: делить не на что, и
    «бесконечный ACWR» в день первой тренировки после простоя - артефакт,
    а не сигнал.
    """
    acute = _rolling_mean(loads, short)
    chronic = _rolling_mean(loads, long)
    return [
        (a / c) if c > 0 else None
        for a, c in zip(acute, chronic)
    ]


def acwr_ewma(loads: list[float], short: int = 7, long: int = 28) -> list[float | None]:
    """EWMA-версия ACWR: сглаженные ряды вместо прямоугольных окон."""
    fast = ewma(loads, short)
    slow = ewma(loads, long)
    return [
        (f / s) if s > 0 else None
        for f, s in zip(fast, slow)
    ]


def monotonicity(loads: list[float]) -> float | None:
    """Монотонность Фостера: средняя дневная нагрузка / её SD.

    Ровная неделя (SD = 0) даёт ``None``, а не бесконечность: однообразие без
    разброса формально и есть максимальная монотонность, но в рядах с нулями
    это почти всегда «не тренировался», и подставлять туда большое число -
    обманывать график.
    """
    if len(loads) < 2:
        return None
    sd = statistics.pstdev(loads)
    if sd == 0:
        return None
    return statistics.fmean(loads) / sd


def strain(week_load: float, monotony: float | None) -> float | None:
    """Стрейн Фостера: недельная нагрузка × монотонность."""
    if monotony is None:
        return None
    return week_load * monotony


def srpe(duration_min: float | None, rpe: float | None) -> float | None:
    """Session-RPE (Foster 1998): длительность в минутах × RPE.

    ``None``, если нет длительности: без неё метрика не определена.
    """
    if duration_min is None or rpe is None:
        return None
    return duration_min * rpe


def weekly_tonnage(tonnages: list[float], window: int = 7) -> list[float]:
    """Скользящая сумма тоннажа за ``window`` дней."""
    out: list[float] = []
    total = 0.0
    for index, value in enumerate(tonnages):
        total += value
        if index >= window:
            total -= tonnages[index - window]
        out.append(total)
    return out


def rm_regress(rm_best: list[float | None]) -> list[int]:
    """Флаг просадки e1RM: недавняя форма ниже предыдущей базы.

    Сравниваются средние по **результатам** (дни без тренировки пропускаются):
    последние :data:`RM_REGRESS_SHORT` результатов против предыдущих
    :data:`RM_REGRESS_LONG`. Если свежая форма ниже базы на
    :data:`RM_REGRESS_DROP`, день помечается - это и есть «тот же вес стал
    тяжелее» из Meeusen et al., один из красных флагов.

    Флаги ставятся только на дни, где результат был: в ряду календаря
    большинство дней - отдых, и размечать их нечем.
    """
    flags = [0] * len(rm_best)
    indices = [i for i, value in enumerate(rm_best) if value is not None]
    values = [rm_best[i] for i in indices]
    for position, index in enumerate(indices):
        recent = values[max(0, position - RM_REGRESS_SHORT + 1):position + 1]
        prior_end = position - RM_REGRESS_SHORT + 1
        prior = values[max(0, prior_end - RM_REGRESS_LONG):max(0, prior_end)]
        if len(prior) < RM_REGRESS_MIN:
            continue
        base = sum(prior) / len(prior)
        form = sum(recent) / len(recent)
        if base > 0 and form < RM_REGRESS_DROP * base:
            flags[index] = 1
    return flags


# --------------------------------------------------------------------------
# День нагрузки
# --------------------------------------------------------------------------


class LoadDay:
    """Одна строка ``load_daily``; обычный контейнер, чтобы не тащить dataclass."""

    __slots__ = (
        "date", "sets", "tonnage", "tonnage_iw", "koi_sum", "rm_best",
        "duration_min", "srpe", "acute", "chronic", "acwr",
        "acute_ewma", "chronic_ewma", "acwr_ewma",
        "weekly_tonnage", "monotony", "strain", "rm_regress", "is_test_day",
    )

    def __init__(self, day: str) -> None:
        self.date = day
        self.sets = 0
        self.tonnage = 0.0
        self.tonnage_iw = 0.0
        self.koi_sum = 0.0
        self.rm_best: float | None = None
        self.duration_min: float | None = None
        self.srpe: float | None = None
        self.acute: float | None = None
        self.chronic: float | None = None
        self.acwr: float | None = None
        self.acute_ewma: float | None = None
        self.chronic_ewma: float | None = None
        self.acwr_ewma: float | None = None
        self.weekly_tonnage = 0.0
        self.monotony: float | None = None
        self.strain: float | None = None
        self.rm_regress = 0
        self.is_test_day = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS load_daily (
  date          TEXT PRIMARY KEY,
  sets          INTEGER NOT NULL DEFAULT 0,
  tonnage       REAL,
  tonnage_iw    REAL,
  koi_sum       REAL,
  rm_best       REAL,
  duration_min  REAL,
  srpe          REAL,
  acute         REAL,
  chronic       REAL,
  acwr          REAL,
  acute_ewma    REAL,
  chronic_ewma  REAL,
  acwr_ewma     REAL,
  weekly_tonnage REAL,
  monotony      REAL,
  strain        REAL,
  rm_regress    INTEGER NOT NULL DEFAULT 0,
  is_test_day   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS health_days (
  date        TEXT PRIMARY KEY,
  trained     INTEGER NOT NULL DEFAULT 0,
  aw_coverage INTEGER NOT NULL DEFAULT 0,
  rhr         REAL,
  hrv_sdnn    REAL,
  sleep_hours REAL,
  in_window   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS test_days (
  date TEXT PRIMARY KEY,
  note TEXT
);
"""

LOAD_COLUMNS: tuple[str, ...] = (
    "date", "sets", "tonnage", "tonnage_iw", "koi_sum", "rm_best",
    "duration_min", "srpe", "acute", "chronic", "acwr",
    "acute_ewma", "chronic_ewma", "acwr_ewma",
    "weekly_tonnage", "monotony", "strain", "rm_regress", "is_test_day",
)


def _d(value: str) -> date_cls:
    return date_cls.fromisoformat(value)


# --------------------------------------------------------------------------
# Материализация
# --------------------------------------------------------------------------


def _session_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """Свести ``sets`` в дневные суммы: подходы, тоннаж, КОИ, лучший e1RM."""
    rows = conn.execute(
        """
        SELECT date,
               COUNT(*)                                          AS sets,
               SUM(CASE WHEN is_warmup = 0 AND skipped = 0
                        THEN weight * reps ELSE 0 END)           AS tonnage,
               SUM(CASE WHEN is_warmup = 0 AND skipped = 0 AND rm_session > 0
                        THEN weight * reps * (weight / rm_session)
                        ELSE 0 END)                              AS tonnage_iw,
               SUM(COALESCE(koi, 0))                             AS koi_sum,
               MAX(rm_session)                                   AS rm_best
        FROM sets
        WHERE date IS NOT NULL
        GROUP BY date
        """
    ).fetchall()
    return {row["date"]: dict(row) for row in rows}


def _workout_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """Длительность и sRPE по силовым тренировкам часов, по датам."""
    if not _has_table(conn, "aw_workouts"):
        return {}
    rows = conn.execute(
        """
        SELECT date,
               SUM(duration_min)                                   AS duration_min,
               SUM(duration_min * COALESCE(effort, effort_estimated, ?)) AS srpe
        FROM aw_workouts
        WHERE activity LIKE '%Strength%' AND duration_min IS NOT NULL
        GROUP BY date
        """,
        (SRPE_DEFAULT_RPE,),
    ).fetchall()
    return {row["date"]: dict(row) for row in rows}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _test_days(conn: sqlite3.Connection) -> set[str]:
    if not _has_table(conn, "test_days"):
        return set()
    return {row["date"] for row in conn.execute("SELECT date FROM test_days")}


def _aw_dates(conn: sqlite3.Connection, window: str) -> list[str]:
    if not _has_table(conn, "aw_daily"):
        return []
    rows = conn.execute(
        "SELECT date FROM aw_daily WHERE date >= ? ORDER BY date", (window,)
    ).fetchall()
    return [row["date"] for row in rows]


def load_daily(
    conn: sqlite3.Connection,
    window: str | None = DEFAULT_WINDOW,
) -> dict[str, LoadDay]:
    """Пересобрать ``load_daily`` и вернуть разобранные дни.

    Ряд ведётся по **непрерывному** календарю: дни без тренировки - это нули,
    а не пропуски. Без нулей скользящие окна ACWR считались бы «сшитыми» через
    месячный простой, и хроническая нагрузка не успевала бы упасть.
    """
    conn.executescript(_SCHEMA)
    sessions = _session_rows(conn)
    workouts = _workout_rows(conn)
    tests = _test_days(conn)
    start = window or None

    all_dates = set(sessions) | set(workouts) | set(_aw_dates(conn, start or "0000"))
    if start:
        all_dates = {d for d in all_dates if d >= start}
    if not all_dates:
        conn.execute("DELETE FROM load_daily")
        conn.commit()
        return {}

    first, last = min(all_dates), max(all_dates)
    calendar = _calendar(first, last)

    days: dict[str, LoadDay] = {}
    for day in calendar:
        item = LoadDay(day)
        session = sessions.get(day)
        if session:
            item.sets = session["sets"] or 0
            item.tonnage = session["tonnage"] or 0.0
            item.tonnage_iw = session["tonnage_iw"] or 0.0
            item.koi_sum = session["koi_sum"] or 0.0
            item.rm_best = session["rm_best"]
        workout = workouts.get(day)
        if workout and workout["duration_min"]:
            item.duration_min = workout["duration_min"]
            item.srpe = workout["srpe"]
        elif item.sets:
            # часов нет - грубая оценка по числу подходов, помечена как суррогат
            item.duration_min = item.sets * SRPE_MIN_PER_SET
            item.srpe = srpe(item.duration_min, SRPE_DEFAULT_RPE)
        item.is_test_day = 1 if day in tests else 0
        days[day] = item

    ordered = [days[d] for d in calendar]
    _apply_windows(ordered)
    _write(conn, ordered)
    health_days(conn, window)
    return days


def _calendar(first: str, last: str) -> list[str]:
    current, stop = _d(first), _d(last)
    out: list[str] = []
    while current <= stop:
        out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _apply_windows(days: list[LoadDay]) -> None:
    """Посчитать скользящие окна по непрерывному ряду."""
    load = [(day.srpe or 0.0) for day in days]
    tonnage = [day.tonnage for day in days]
    rm_best = [day.rm_best for day in days]

    acute = _rolling_mean(load, 7)
    chronic = _rolling_mean(load, 28)
    fast = ewma(load, 7)
    slow = ewma(load, 28)
    week = weekly_tonnage(tonnage, 7)
    flags = rm_regress(rm_best)

    for index, day in enumerate(days):
        day.acute = acute[index]
        day.chronic = chronic[index]
        day.acwr = (acute[index] / chronic[index]) if chronic[index] > 0 else None
        day.acute_ewma = fast[index]
        day.chronic_ewma = slow[index]
        day.acwr_ewma = (fast[index] / slow[index]) if slow[index] > 0 else None
        day.weekly_tonnage = week[index]
        day.rm_regress = flags[index]
        window = load[max(0, index - 6):index + 1]
        day.monotony = monotonicity(window)
        day.strain = strain(week[index], day.monotony)


def _write(conn: sqlite3.Connection, days: list[LoadDay]) -> None:
    conn.execute("DELETE FROM load_daily")
    placeholders = ", ".join("?" * len(LOAD_COLUMNS))
    conn.executemany(
        f"INSERT INTO load_daily ({', '.join(LOAD_COLUMNS)}) VALUES ({placeholders})",
        [tuple(getattr(day, col) for col in LOAD_COLUMNS) for day in days],
    )
    conn.commit()


def health_days(
    conn: sqlite3.Connection,
    window: str | None = DEFAULT_WINDOW,
) -> int:
    """Пересобрать ``health_days``: непрерывный календарь с признаком тренировки.

    Возвращает число дней. Ряд тянется от нагрузки к часам и обратно, чтобы
    у ML были и дни отдыха: болезнь приходит и в дни без тренировок, и модель
    без отрицательных примеров училась бы только на «тренировался -> болен».
    """
    conn.executescript(_SCHEMA)
    has_aw = _has_table(conn, "aw_daily")
    aw_dates = set(_aw_dates(conn, window or "0000")) if has_aw else set()
    trained = {row["date"] for row in conn.execute(
        "SELECT DISTINCT date FROM sets WHERE date IS NOT NULL")}

    all_dates = trained | aw_dates
    if window:
        all_dates = {d for d in all_dates if d >= window}
    conn.execute("DELETE FROM health_days")
    if not all_dates:
        conn.commit()
        return 0

    first, last = min(all_dates), max(all_dates)
    rows: list[tuple] = []
    for day in _calendar(first, last):
        rows.append((day, 1 if day in trained else 0, 1 if day in aw_dates else 0,
                     None, None, None, 1))
    conn.executemany(
        "INSERT INTO health_days (date, trained, aw_coverage, rhr, hrv_sdnn, "
        "sleep_hours, in_window) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    if has_aw:
        conn.executescript(
            "UPDATE health_days SET "
            "rhr = (SELECT rhr FROM aw_daily WHERE aw_daily.date = health_days.date), "
            "hrv_sdnn = (SELECT hrv_sdnn FROM aw_daily WHERE aw_daily.date = health_days.date), "
            "sleep_hours = (SELECT sleep_hours FROM aw_daily WHERE aw_daily.date = health_days.date)"
        )
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------
# Отчёт
# --------------------------------------------------------------------------


def summarize(conn: sqlite3.Connection) -> dict:
    """Сводка по ``load_daily`` для CLI и ноутбука."""
    row = conn.execute(
        """
        SELECT COUNT(*)                         AS days,
               SUM(sets)                        AS sets,
               SUM(tonnage)                     AS tonnage,
               AVG(acwr)                        AS acwr,
               AVG(monotony)                    AS monotony,
               MAX(acwr)                        AS acwr_max,
               MAX(monotony)                    AS monotony_max,
               SUM(CASE WHEN monotony > ? THEN 1 ELSE 0 END) AS monotony_alert,
               SUM(rm_regress)                  AS rm_regress
        FROM load_daily
        """,
        (MONOTONY_ALERT,),
    ).fetchone()
    period = conn.execute(
        "SELECT MIN(date) AS first, MAX(date) AS last FROM load_daily"
    ).fetchone()
    return {**dict(row), "first": period["first"], "last": period["last"]}


def format_summary(conn: sqlite3.Connection) -> str:
    """Готовая сводка ``load_daily`` для CLI и ноутбука."""
    summary = summarize(conn)
    if not summary["days"]:
        return "load_daily пуст: сначала  python -m koilog health-load"
    lines = [
        f"дней в ряду: {summary['days']}  "
        f"({summary['first']} … {summary['last']})",
        f"подходов: {summary['sets']}   тоннаж: {summary['tonnage']:.0f} кг",
    ]
    if summary["acwr"] is not None:
        lines.append(f"ACWR: средний {summary['acwr']:.2f}, "
                     f"макс {summary['acwr_max']:.2f}")
    if summary["monotony"] is not None:
        lines.append(f"монотонность: средняя {summary['monotony']:.2f}, "
                     f"макс {summary['monotony_max']:.2f}, "
                     f"дней > {MONOTONY_ALERT:g}: {summary['monotony_alert']}")
    lines.append(f"дней с просадкой e1RM: {summary['rm_regress']}")
    return "\n".join(lines)


def series(
    conn: sqlite3.Connection, window: str | None = DEFAULT_WINDOW
) -> list[dict]:
    """Ряд нагрузки по дням для графиков в ноутбуке."""
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    rows = conn.execute(
        f"""SELECT date, sets, tonnage, srpe, acute, chronic, acwr, acwr_ewma,
                   weekly_tonnage, monotony, strain, rm_regress
            FROM load_daily {where} ORDER BY date""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]
