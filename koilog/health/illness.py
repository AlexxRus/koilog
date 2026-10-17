"""Многоуровневые метки болезни из трёх независимых источников.

Разметки в журнале нет (колонка ``ЗАМЕТКА`` бесполезна), поэтому эпизоды
собираются из того, что действительно измеримо:

``confirmed``
    Ручной термометр ``BodyTemperature ≥ 37.5 °C`` **или** сигнатура часов:
    за один день сработали ≥ 2 условия из {пульс покоя z>2, ВСР z<−2,
    ЧДД z>2, температура запястья z>2.5}. Сигнатура - достоверная картина
    гриппоподобного эпизода (Mishra 2020; Quer 2021; Radin 2020).
``probable``
    Плотный дневник температур (≥ 2 измерения в день ≥ 2 дней подряд),
    **или** одиночный сильный сигнал часов (одно условие с порогом z>3),
    **или** перерыв в журнале 14–30 дней с отклонением часов внутри.
``gap_only``
    Перерыв ≥ 14 дней без температурного и часового подтверждения: отпуск,
    лень, командировка. В обучение **не идёт**, но показывается отдельно,
    чтобы было видно, что метки его не «съедают».

z-скоры личные: значение сравнивается с собственным фоном за предыдущие
60 дней (минимум 14 наблюдений), сам текущий день в фон не входит. Это важно
при ``n=1``: абсолютные пороги пульса у всех разные, а отклонение от своего
фона - нет.

Тип эпизода (ОРВИ/грипп/COVID/травма) - ручной мультикласс, по умолчанию
``unknown``; переопределяется таблицей ``illness_manual``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import timedelta

from . import load as health_load
from .text import table

__all__ = [
    "CONFIRMED_TEMP",
    "GAP_MIN_DAYS",
    "Episode",
    "breach_matrix",
    "build",
    "core_strong",
    "episodes",
    "find_gaps",
    "merge_episodes",
    "summarize",
    "watch_breaches",
    "zscore",
]

#: Порог ручного термометра для подтверждённого эпизода.
CONFIRMED_TEMP = 37.5

#: Минимальная длина перерыва в журнале, чтобы он считался «перерывом».
GAP_MIN_DAYS = 14

#: z-пороги сигнатуры часов (подтверждённый уровень).
Z_RHR = 2.0
Z_HRV = -2.0
Z_RESP = 2.0
Z_TEMP = 2.5

#: Порог «одиночного сильного сигнала» (вероятный уровень).
Z_STRONG = 3.0

#: Окно личного базлайна и минимум наблюдений в нём.
BASELINE_DAYS = 60
BASELINE_MIN = 14

#: Метрики, которые в кузове «автономного» ответа: их изменение и есть
#: документированная сигнатура болезни (пульс покоя +12…15 уд, ВСР −35 мс).
#: ``resp`` и ``wrist`` - одноканальные ночные метрики с большим разбросом
#: на обычных днях (sd 0,77 дыхания/мин и 0,29 °C), поэтому одного их
#: выброса для эпизода мало: они подтверждают, а не открывают.
CORE_KEYS = ("rhr", "hrv")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS illness_episodes (
  id     INTEGER PRIMARY KEY,
  start  TEXT NOT NULL,
  end    TEXT NOT NULL,
  level  TEXT NOT NULL,
  source TEXT,
  type   TEXT NOT NULL DEFAULT 'unknown',
  notes  TEXT
);

CREATE TABLE IF NOT EXISTS illness_day (
  date       TEXT PRIMARY KEY,
  level      TEXT NOT NULL,
  episode_id INTEGER REFERENCES illness_episodes(id)
);

CREATE TABLE IF NOT EXISTS illness_manual (
  start TEXT PRIMARY KEY,
  type  TEXT NOT NULL DEFAULT 'unknown',
  notes TEXT
);
"""


# --------------------------------------------------------------------------
# Чистые функции
# --------------------------------------------------------------------------


def zscore(
    values: list[float | None],
    window: int = BASELINE_DAYS,
    min_periods: int = BASELINE_MIN,
) -> list[float | None]:
    """z-скор каждого значения к собственному фону за предыдущие ``window`` дней.

    Фон - строго прошлые значения (текущий день не входит): иначе сильный
    выброс сам раздувает SD и маскирует себя. ``None``, если наблюдений
    в фон меньше ``min_periods`` или фон нулевой.
    """
    out: list[float | None] = []
    history: list[float] = []
    for value in values:
        if len(history) >= min_periods:
            mean = sum(history) / len(history)
            var = sum((x - mean) ** 2 for x in history) / len(history)
            sd = var ** 0.5
            out.append((value - mean) / sd if value is not None and sd > 0 else None)
        else:
            out.append(None)
        if value is not None:
            history.append(value)
            if len(history) > window:
                history.pop(0)
    return out


#: Проверки сигнатуры: ``(метрика, порог умеренного, порог сильного)``.
#: Направления зашиты: пульс, ЧДД и температура растут, ВСР падает - так
#: устроена гриппоподобная реакция (Mishra 2020, Quer 2021, Radin 2020).
_WATCH_CHECKS = (
    ("rhr", Z_RHR, Z_STRONG),
    ("hrv", Z_HRV, -Z_STRONG),
    ("resp", Z_RESP, Z_STRONG),
    ("temp", Z_TEMP, Z_STRONG),
)


def _breach_state(value: float | None, threshold: float, strong: float) -> str:
    """``none`` / ``moderate`` / ``strong`` для одной метрики."""
    if value is None:
        return "none"
    if threshold < 0:
        if value < strong:
            return "strong"
        if value < threshold:
            return "moderate"
        return "none"
    if value > strong:
        return "strong"
    if value > threshold:
        return "moderate"
    return "none"


def breach_matrix(
    rhr: float | None,
    hrv: float | None,
    resp: float | None,
    temp: float | None,
) -> dict[str, str]:
    """По-метричное состояние сигнатуры: ``none`` / ``moderate`` / ``strong``.

    Нужно, чтобы отличать автономное ядро (RHR/ВСР) от шумных одноканальных
    ночных метрик (ЧДД, температура запястья) при правиле «одиночный сильный
    сигнал» (см. :func:`core_strong`).
    """
    values = {"rhr": rhr, "hrv": hrv, "resp": resp, "temp": temp}
    return {
        key: _breach_state(values[key], threshold, strong)
        for key, threshold, strong in _WATCH_CHECKS
    }


def core_strong(matrix: dict[str, str]) -> int:
    """Сколько сильных сигналов среди автономного ядра (RHR/ВСР).

    ЧДД (SD ≈ 0,77 дых/мин) и температура запястья (SD ≈ 0,29 °C) на обычных
    днях шумят сильнее документированной сигнатуры, поэтому их одиночный выброс
    эпизод не открывает - они лишь подтверждают при ≥2 метриках. Ядро RHR/ВСР
    меняется при болезни на +12…15 уд и −35 мс и специфично.
    """
    return sum(1 for key in CORE_KEYS if matrix[key] == "strong")


def watch_breaches(
    rhr: float | None,
    hrv: float | None,
    resp: float | None,
    temp: float | None,
) -> tuple[int, int]:
    """Сколько условий сигнатуры сработало: ``(умеренных, сильных)``.

    Умеренный включает и сильный. Счётчики нужны для ``confirmed`` (≥2).
    Направления зашиты: пульс, ЧДД и температура растут, ВСР падает.
    """
    states = breach_matrix(rhr, hrv, resp, temp).values()
    moderate = sum(1 for state in states if state != "none")
    strong = sum(1 for state in states if state == "strong")
    return moderate, strong


def find_gaps(trained: list[str], min_days: int = GAP_MIN_DAYS) -> list[tuple[str, str]]:
    """Перерывы в журнале: пары ``(первый_день_без_тренировки, последний)``.

    Перерыв считается от дня после последней тренировки до дня перед
    следующей. ``min_days`` - минимальное число пропущенных дней подряд.
    """
    days = sorted(set(trained))
    gaps: list[tuple[str, str]] = []
    for prev, nxt in zip(days, days[1:]):
        start = date_cls.fromisoformat(prev) + timedelta(days=1)
        end = date_cls.fromisoformat(nxt) - timedelta(days=1)
        if (end - start).days + 1 >= min_days:
            gaps.append((start.isoformat(), end.isoformat()))
    return gaps


def merge_episodes(
    day_levels: dict[str, str],
    gaps: list[tuple[str, str]],
    gap_levels: dict[str, str] | None = None,
) -> list["Episode"]:
    """Склеить соседние помеченные дни в эпизоды.

    Соседство - по календарю: день примыкает к предыдущему, если он ровно на
    сутки позже. Уровень эпизода - максимальный из его дней (``confirmed`` >
    ``probable`` > ``gap_only``). Перерывы без подтверждения попадают сюда как
    ``gap_only`` и не перекрывают уже найденные температурные/часовые эпизоды.
    """
    episodes: list[Episode] = []
    if day_levels:
        for day in sorted(day_levels):
            level = day_levels[day]
            if episodes and _adjacent(episodes[-1].end, day):
                episodes[-1].end = day
                episodes[-1].level = _max_level(episodes[-1].level, level)
            else:
                episodes.append(Episode(day, day, level, "watch/temp"))
    for start, end in gaps:
        if _covered(day_levels, start, end):
            continue
        level = (gap_levels or {}).get(start, "gap_only")
        source = "gap+watch" if level != "gap_only" else "gap"
        episodes.append(Episode(start, end, level, source))
    episodes.sort(key=lambda e: e.start)
    return episodes


def _adjacent(prev_end: str, day: str) -> bool:
    return date_cls.fromisoformat(day) - date_cls.fromisoformat(prev_end) == timedelta(days=1)


def _covered(day_levels: dict[str, str], start: str, end: str) -> bool:
    """Есть ли внутри перерыва хоть один уже помеченный день."""
    current = date_cls.fromisoformat(start)
    stop = date_cls.fromisoformat(end)
    while current <= stop:
        if current.isoformat() in day_levels:
            return True
        current += timedelta(days=1)
    return False


_LEVEL_ORDER = {"gap_only": 0, "probable": 1, "confirmed": 2}


def _max_level(left: str, right: str) -> str:
    return left if _LEVEL_ORDER[left] >= _LEVEL_ORDER[right] else right


@dataclass(slots=True)
class Episode:
    """Эпизод болезни: интервал, уровень, источник, тип."""

    start: str
    end: str
    level: str
    source: str
    type: str = "unknown"
    notes: str | None = None
    id: int | None = None

    @property
    def days(self) -> int:
        return (date_cls.fromisoformat(self.end) - date_cls.fromisoformat(self.start)).days + 1


# --------------------------------------------------------------------------
# Материализация
# --------------------------------------------------------------------------


def _temp_stats(conn: sqlite3.Connection, window: str | None) -> dict[str, tuple[int, float]]:
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    rows = conn.execute(
        f"SELECT date, COUNT(*) AS n, MAX(value) AS peak FROM aw_bodytemp {where} "
        f"GROUP BY date",
        params,
    ).fetchall()
    return {row["date"]: (row["n"], row["peak"] or 0.0) for row in rows}


def _daily_rows(conn: sqlite3.Connection, window: str | None) -> list[dict]:
    where = "WHERE date >= ?" if window else ""
    params = (window,) if window else ()
    rows = conn.execute(
        f"SELECT date, rhr, hrv_sdnn, resp_rate, wrist_temp, sleep_hours "
        f"FROM aw_daily {where} ORDER BY date",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _day_levels(conn: sqlite3.Connection, window: str | None) -> tuple[dict, dict]:
    """Разметить дни по температуре и сигнатуре часов.

    Возвращает ``(уровни_дней, дни_с_отклонением_часов)``. Второе нужно, чтобы
    отличить перерыв с физиологической активностью (``probable``) от простого
    прогула (``gap_only``).
    """
    rows = _daily_rows(conn, window)
    temps = _temp_stats(conn, window)
    z_rhr = zscore([r["rhr"] for r in rows])
    z_hrv = zscore([r["hrv_sdnn"] for r in rows])
    z_resp = zscore([r["resp_rate"] for r in rows])
    z_temp = zscore([r["wrist_temp"] for r in rows])

    levels: dict[str, str] = {}
    deviations: dict[str, bool] = {}

    dense = _dense_temp_days(temps)
    for index, row in enumerate(rows):
        day = row["date"]
        level = "probable" if day in dense else None

        peak = temps.get(day, (0, 0.0))[1]
        if peak >= CONFIRMED_TEMP:
            level = "confirmed"

        moderate, strong = watch_breaches(
            z_rhr[index], z_hrv[index], z_resp[index], z_temp[index])
        matrix = breach_matrix(
            z_rhr[index], z_hrv[index], z_resp[index], z_temp[index])
        if moderate >= 1 or strong >= 1:
            deviations[day] = True
        if moderate >= 2:
            level = "confirmed"
        elif level != "confirmed" and core_strong(matrix) >= 1:
            # «одиночный сильный сигнал» (§5) - но только по автономному
            # ядру RHR/ВСР. Шумные ЧДД/температура при z>3 в одиночку эпизод
            # не открывают: они лишь подтверждают при ≥2 метриках (см.
            # core_strong). Иначе в отчёт попадают десятки разовых дней.
            level = "probable"

        if level:
            levels[day] = level
    return levels, deviations


def _dense_temp_days(temps: dict[str, tuple[int, float]]) -> set[str]:
    """Дни, входящие в серию ≥ 2 дней, где замеряли температуру ≥ 2 раз в день."""
    crowded = sorted(day for day, (n, _) in temps.items() if n >= 2)
    dense: set[str] = set()
    run: list[str] = []
    for day in crowded:
        if run and _adjacent(run[-1], day):
            run.append(day)
        else:
            if len(run) >= 2:
                dense.update(run)
            run = [day]
    if len(run) >= 2:
        dense.update(run)
    return dense


def _gap_levels(
    gaps: list[tuple[str, str]],
    levels: dict[str, str],
    deviations: dict[str, bool],
) -> dict[str, str]:
    """Уровень для каждого перерыва: ``probable`` при отклонении часов внутри."""
    out: dict[str, str] = {}
    for start, end in gaps:
        if _covered(levels, start, end):
            continue
        out[start] = "probable" if _has_deviation(deviations, start, end) else "gap_only"
    return out


def _has_deviation(deviations: dict[str, bool], start: str, end: str) -> bool:
    current = date_cls.fromisoformat(start)
    stop = date_cls.fromisoformat(end)
    while current <= stop:
        if deviations.get(current.isoformat()):
            return True
        current += timedelta(days=1)
    return False


def build(conn: sqlite3.Connection, window: str | None = health_load.DEFAULT_WINDOW) -> list[Episode]:
    """Пересобрать ``illness_episodes`` и ``illness_day``; вернуть эпизоды."""
    conn.executescript(_SCHEMA)
    if not _has_table(conn, "aw_daily"):
        raise RuntimeError(
            "нет таблицы aw_daily - сначала:  python -m koilog apple-load"
        )

    levels, deviations = _day_levels(conn, window)
    trained = [row["date"] for row in conn.execute(
        "SELECT DISTINCT date FROM sets WHERE date IS NOT NULL")]
    gaps = find_gaps(trained)
    if window:
        gaps = [(s, e) for s, e in gaps if e >= window]
    gap_levels = _gap_levels(gaps, levels, deviations)
    episodes = merge_episodes(levels, gaps, gap_levels)

    manual = _manual(conn)
    conn.execute("DELETE FROM illness_day")
    conn.execute("DELETE FROM illness_episodes")
    for index, episode in enumerate(episodes, start=1):
        manual_entry = manual.get(episode.start)
        if manual_entry:
            episode.type, episode.notes = manual_entry
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (index, episode.start, episode.end, episode.level, episode.source,
             episode.type, episode.notes),
        )
        episode.id = index
        current = date_cls.fromisoformat(episode.start)
        stop = date_cls.fromisoformat(episode.end)
        while current <= stop:
            conn.execute(
                "INSERT OR REPLACE INTO illness_day (date, level, episode_id) "
                "VALUES (?, ?, ?)",
                (current.isoformat(), episode.level, index),
            )
            current += timedelta(days=1)
    conn.commit()
    return episodes


def _manual(conn: sqlite3.Connection) -> dict[str, tuple[str, str | None]]:
    if not _has_table(conn, "illness_manual"):
        return {}
    return {
        row["start"]: (row["type"], row["notes"])
        for row in conn.execute("SELECT start, type, notes FROM illness_manual")
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def episodes(conn: sqlite3.Connection) -> list[Episode]:
    """Прочитать сохранённые эпизоды."""
    rows = conn.execute(
        "SELECT id, start, end, level, source, type, notes "
        "FROM illness_episodes ORDER BY start"
    ).fetchall()
    return [Episode(row["start"], row["end"], row["level"], row["source"] or "",
                    row["type"], row["notes"], row["id"]) for row in rows]


def summarize(conn: sqlite3.Connection) -> dict:
    """Счётчики по уровням и список эпизодов."""
    rows = episodes(conn)
    counts: dict[str, int] = {}
    for episode in rows:
        counts[episode.level] = counts.get(episode.level, 0) + 1
    return {"total": len(rows), "by_level": counts, "episodes": rows}


def format_episodes(conn: sqlite3.Connection) -> str:
    """Готовая таблица эпизодов для CLI и ноутбука."""
    summary = summarize(conn)
    by_level = summary["by_level"]
    head = (
        f"эпизодов: {summary['total']}  "
        f"(подтверждённых {by_level.get('confirmed', 0)}, "
        f"вероятных {by_level.get('probable', 0)}, "
        f"только перерыв {by_level.get('gap_only', 0)})"
    )
    rows = [
        (e.start, e.end, e.days, e.level, e.source, e.type)
        for e in summary["episodes"]
    ]
    body = table(("с", "по", "дней", "уровень", "источник", "тип"), rows)
    return f"{head}\n\n{body}" if body else head
