"""Готовые срезы данных для ноутбука и разведки (stdlib-only).

Здесь нет ни моделей, ни статистики за пределами среднего/квантили/SD - только
выборки рядов, которые ноутбук рисует как есть. Принцип тот же, что в ядре:
логика живёт в пакете, ячейки ноутбука только вызывают эти функции и строят
график. Всё держится на стандартной библиотеке, поэтому раздел работает и без
extra ``ml``.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date as date_cls, timedelta

from . import illness as health_illness
from . import load as health_load

__all__ = [
    "episodes_frame",
    "pre_episode_windows",
    "quantile_profile",
    "signature",
]

#: Число дней до начала эпизода, которые берутся в «спагетти»-окно.
PRE_DAYS = 28

#: Каналы внутренней нагрузки, которые рисуются в сигнатуре.
_CHANNELS = (
    ("rhr", "RHR, уд/мин"),
    ("hrv_sdnn", "ВСР, мс"),
    ("resp_rate", "ЧДД"),
    ("wrist_temp", "t запястья, °C"),
    ("sleep_hours", "сон, ч"),
)


def _d(value: str) -> date_cls:
    return date_cls.fromisoformat(value)


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def signature(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
    level: str = "confirmed",
) -> list[dict]:
    """Среднее по каналам для каждого эпизода и для «обычных» дней.

    Возвращает по строке на эпизод: даты, уровень и среднее каждого канала
    внутри эпизода; плюс отдельную строку ``"обычные"`` со средним по дням вне
    любых эпизодов - это фон, с которым сравнивается сигнатура (§1.2 плана:
    RHR ↑, ВСР ↓, ЧДД ↑, температура ↑, сон ↓).
    """
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    daily = _rows(
        conn,
        f"SELECT date, rhr, hrv_sdnn, resp_rate, wrist_temp, sleep_hours "
        f"FROM aw_daily {where} ORDER BY date",
        params,
    )
    episodes = [e for e in health_illness.episodes(conn) if e.level == level]
    by_date = {row["date"]: row for row in daily}

    def mean_over(dates: list[str], channel: str) -> float | None:
        values = [by_date[d][channel] for d in dates
                  if d in by_date and by_date[d][channel] is not None]
        return round(statistics.fmean(values), 2) if values else None

    def days_between(start: str, end: str) -> list[str]:
        current, stop = _d(start), _d(end)
        out = []
        while current <= stop:
            out.append(current.isoformat())
            current += timedelta(days=1)
        return out

    sick_days: set[str] = set()
    out: list[dict] = []
    for episode in episodes:
        dates = days_between(episode.start, episode.end)
        sick_days.update(dates)
        row = {"start": episode.start, "end": episode.end,
               "level": episode.level, "days": len(dates)}
        row.update({label: mean_over(dates, channel)
                    for channel, label in _CHANNELS})
        out.append(row)

    normal = [row["date"] for row in daily if row["date"] not in sick_days]
    if normal:
        row = {"start": "—", "end": "обычные дни", "level": "фон",
               "days": len(normal)}
        row.update({label: mean_over(normal, channel)
                    for channel, label in _CHANNELS})
        out.append(row)
    return out


def episodes_frame(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
) -> list[dict]:
    """Строка на эпизод: длительность, уровень, источник, тип."""
    episodes = health_illness.episodes(conn)
    if window:
        episodes = [e for e in episodes if e.end >= window]
    return [{"id": e.id, "start": e.start, "end": e.end, "days": e.days,
             "level": e.level, "source": e.source, "type": e.type}
            for e in episodes]


def pre_episode_windows(
    conn: sqlite3.Connection,
    feature: str = "acwr",
    window: str | None = health_load.DEFAULT_WINDOW,
    pre_days: int = PRE_DAYS,
    max_control: int | None = None,
) -> dict:
    """Ряд признака за ``pre_days`` до каждого эпизода, выровненный по лагу.

    Возвращает словарь: ``lags`` (0 = день до начала, ``pre_days`` = самый
    ранний), ``series`` - по одному ряду значений на эпизод (на позиции лага
    может быть ``None``, если данных нет), ``mean`` - среднее по эпизодам на
    каждую позицию. Это спагетти-график: каждый эпизод - линия, ``mean`` -
    общий тренд перед болезнью.

    Рядом строятся **контрольные окна** (``control``/``control_mean``): те же
    смещения от дней, далёких от любого эпизода (по ``pre_days`` до и после).
    Без них «спагетти» невозможно отличить предболезненный тренд от обычной
    дневной вариативности признака. Контрольные якоря берутся равномерно и не
    больше числа эпизодов (иначе серые линии заглушают сигнал).
    """
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    rows = _rows(conn, f"SELECT date, {feature} FROM ml_features {where} "
                       f"ORDER BY date", params)
    values = {row["date"]: row[feature] for row in rows}
    dates = [row["date"] for row in rows]
    index_of = {day: i for i, day in enumerate(dates)}
    eps = [e for e in health_illness.episodes(conn) if e.level != "gap_only"]
    onsets = [e.start for e in eps if e.start in index_of]

    lags = list(range(pre_days, -1, -1))

    def window_at(center: int) -> list[float | None]:
        start = center - pre_days
        if start < 0:
            return [None] * len(lags)
        return [values[dates[center - lag]] for lag in lags]

    series = [window_at(index_of[onset]) for onset in onsets]

    # дни, чья близость к эпизоду запрещает контроль: [onset-pre_days, end+pre_days]
    banned: set[int] = set()
    for episode in eps:
        start_i = index_of.get(episode.start)
        if start_i is None:
            continue
        end_i = index_of.get(episode.end, start_i)
        for j in range(max(0, start_i - pre_days),
                       min(len(dates), end_i + pre_days + 1)):
            banned.add(j)

    eligible = [i for i in range(pre_days, len(dates))
                if all(j not in banned for j in range(i - pre_days, i + 1))]
    limit = max_control if max_control is not None else max(1, len(series))
    if eligible:
        step = max(1, len(eligible) // limit)
        control_centers = eligible[::step][:limit]
    else:
        control_centers = []
    control = [window_at(i) for i in control_centers]

    def mean_over(series_list: list[list[float | None]]) -> list[float | None]:
        out = []
        for column in range(len(lags)):
            picked = [s[column] for s in series_list if s[column] is not None]
            out.append(round(statistics.fmean(picked), 3) if picked else None)
        return out

    return {
        "lags": lags, "series": series, "mean": mean_over(series),
        "control": control, "control_mean": mean_over(control),
        "n": len(series), "n_control": len(control),
    }


def quantile_profile(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
    quantiles: tuple[float, ...] = (0.25, 0.5, 0.75),
) -> list[dict]:
    """Канальные квантили в «предболезненные» дни против обычных.

    «Предболезненные» - дни с ``y=1`` в ``ml_features`` (за ``HORIZON`` до
    начала эпизода). Считаются квантили каждого канала в этих днях и в днях
    ``y=0``; строка ``delta`` показывает, на сколько квантилей предболезненные
    дни сдвинуты относительно обычных (§8 плана - персональные квантили).
    """
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    daily = _rows(
        conn,
        f"SELECT date, rhr, hrv_sdnn, resp_rate, wrist_temp, sleep_hours "
        f"FROM aw_daily {where} ORDER BY date",
        params,
    )
    labels = {row["date"]: row["y"] for row in _rows(
        conn, "SELECT date, y FROM ml_labels")}
    sick = {row["date"] for row in daily if labels.get(row["date"]) == 1}
    normal = {row["date"] for row in daily
              if row["date"] not in sick and labels.get(row["date"], 0) == 0}

    def picked(dates: set[str], channel: str) -> list[float]:
        by_date = {row["date"]: row for row in daily}
        return [by_date[d][channel] for d in sorted(dates)
                if d in by_date and by_date[d][channel] is not None]

    out: list[dict] = []
    for channel, label in _CHANNELS:
        before = picked(sick, channel)
        after = picked(normal, channel)
        if not before or not after:
            continue
        row = {"channel": label}
        for q in quantiles:
            name = f"q{int(q * 100):02d}"
            row[name] = round(statistics.quantiles(after, n=100)[int(q * 100) - 1], 2)
            row[f"{name}_pre"] = round(
                statistics.quantiles(before, n=100)[int(q * 100) - 1], 2)
        row["median_shift"] = round(
            (statistics.median(before) - statistics.median(after))
            / (statistics.pstdev(after) or 1.0), 2)
        out.append(row)
    return out
