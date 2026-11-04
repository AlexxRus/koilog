"""Восходящая дуга относительной работы (RW-98) — display-only.

Пререг: ``docs/rw-98-prereg.md`` (принят 2026-11-04). Показатель — недельная
работа поверхности (группа или упражнение), нормированная на вес тела года
недели; дуга — дельта к предыдущей неделе **с данными**; остановки — оверлей
существующих сигналов (ACWR gap-aware и недельные КОИ-полосы).

Определения (R1-R8):

* **R(w)** — ``E-тоннаж недели / вес_тела(год недели)``. E-тоннаж = сумма
  ``weight * reps`` по рабочим подходам (``is_warmup = 0 AND skipped = 0 AND
  is_outlier = 0 AND is_foreign = 0``); ``weight`` после RM-98 уже эффективный
  (k·тело + добавка у ``vest_only``), поэтому «сопоставимые кг» здесь — прямые
  килограммы столбца.
* **вес тела** — ``estimate_bodyweight`` (одно значение в год, 12 значений на
  живой базе), фоллбэк ``core.BODY_WEIGHT`` для года без оценки.
* **ΔR** — ``R(неделя) − R(предыдущая неделя с данными)``; первая неделя
  поверхности без дельты (``None``). Разрывы не шьются — дельта честно прыгает
  через простой и только **аннотируется** ``after_gap`` по ``load_daily``.
* **стоп** — OR от ``acwr_warmup > ACWR_ALERT`` (порог переиспользован из
  :mod:`koilog.health.digest`) и недельной полосы КОИ `≥ «Очень тяжёлый»`
  (``KOI_BANDS_WEEK[-1]`` + ``KOI_ABOVE_WEEK``, читается из ``v_weekly_koi``,
  не пересчитывается).

Неделя — ISO 8601 (``date.isocalendar()``, метка ``ГГГГ-Www``) — согласована с
``etl.iso_week_sql`` и колонкой ``week`` вью ``v_weekly_koi``. Модуль **ничего
не пишет** и не входит в решения (state/gate/ramp/digest/score/ml*) — фенс
display-only, проверяется import-фенсом в тестах.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls

from . import taxonomy
from .core import BODY_WEIGHT, KOI_ABOVE_WEEK, KOI_BANDS_WEEK
from .etl import estimate_bodyweight
from .health.digest import ACWR_ALERT

__all__ = [
    "WeekPoint",
    "arc",
    "format_arc",
    "surfaces",
    "week_of",
    "weekly_tonnage",
]

#: Полосы недельного КОИ, которые считаются остановкой (R6-α: ≥ «Очень
#: тяжёлый»). Собраны из ``core.KOI_BANDS_WEEK[-1][1]`` (последняя обычная
#: полоса) и ``core.KOI_ABOVE_WEEK`` (всё, что выше) — литералов ``3``/``4`` в
#: модуле нет, порог переиспользуется.
HEAVY_BANDS: frozenset[str] = frozenset({
    KOI_BANDS_WEEK[-1][1],
    KOI_ABOVE_WEEK,
})

#: Ранг полосы для выбора «тяжелейшей» в неделе-поверхности (только дискретная
#: подпись, решений не принимает).
_BAND_RANK: dict[str, int] = {
    name: index for index, (_, name) in enumerate(KOI_BANDS_WEEK)
}
_BAND_RANK[KOI_ABOVE_WEEK] = len(KOI_BANDS_WEEK)


def week_of(day: str) -> str:
    """ISO-неделя дня: ``ГГГГ-Www`` (четверг определяет год и номер).

    Совпадает с ``etl.iso_week_sql`` и колонкой ``week`` в ``v_weekly_koi``;
    ``muscle_load.week_key`` (``%Y-W%W``) тут не годится — он даёт W00 на стыке
    года (29-31.12 + 1-3.01 должны быть одной неделей, а не двумя).
    """
    iso = date_cls.fromisoformat(day).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _pool(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Рабочие подходы: без разминки, пропусков, выбросов и чужих строк."""
    return conn.execute(
        """
        SELECT date, exercise, weight, reps
          FROM sets
         WHERE date IS NOT NULL
           AND is_warmup = 0
           AND skipped = 0
           AND is_outlier = 0
           AND is_foreign = 0
        """
    ).fetchall()


def _tonnage(
    rows: list[sqlite3.Row],
    *,
    group: str | None = None,
    exercise: str | None = None,
) -> dict[str, float]:
    """E-тоннаж недели по поверхности: ``Σ weight×reps`` (``weight`` = E)."""
    tonnage: dict[str, float] = defaultdict(float)
    for row in rows:
        work = (row["weight"] or 0.0) * float(row["reps"] or 0)
        if work <= 0:
            continue
        name = row["exercise"]
        if exercise is not None:
            if name != exercise:
                continue
        elif group is not None:
            if taxonomy.group_of(name).name != group:
                continue
        tonnage[week_of(row["date"])] += work
    return dict(tonnage)


def weekly_tonnage(
    conn: sqlite3.Connection,
    *,
    group: str | None = None,
    exercise: str | None = None,
) -> dict[str, float]:
    """E-тоннаж ``{ISO-неделя: кг}`` для поверхности (группа или упражнение).

    ``group`` и ``exercise`` взаимоисключают; если оба заданы — ``ValueError``.
    """
    if group is not None and exercise is not None:
        raise ValueError("поверхность: либо group, либо exercise, не оба")
    if group is None and exercise is None:
        return {}
    return _tonnage(_pool(conn), group=group, exercise=exercise)


def surfaces(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Доступные поверхности для селектора: ``(группы, упражнения)``.

    В список попадают только поверхности с ненулевым E-тоннажом (пустая
    поверхность в дугу не рендерится). Группа — ``taxonomy.group_of().name``
    без дельта-разбивки (R3-C отклонена).
    """
    groups: set[str] = set()
    exercises: set[str] = set()
    for row in _pool(conn):
        if (row["weight"] or 0.0) * float(row["reps"] or 0) <= 0:
            continue
        exercises.add(row["exercise"])
        groups.add(taxonomy.group_of(row["exercise"]).name)
    return sorted(groups), sorted(exercises)


def _acwr_overlay(conn: sqlite3.Connection) -> tuple[dict[str, float], set[str]]:
    """``({неделя: MAX(acwr_warmup)}, {недели после разрыва})``."""
    acwr_by_week: dict[str, float] = {}
    gap_weeks: set[str] = set()
    for row in conn.execute(
        "SELECT date, acwr_warmup, gap_warmup FROM load_daily"
    ):
        if row["date"] is None:
            continue
        week = week_of(row["date"])
        value = row["acwr_warmup"]
        if value is not None and (week not in acwr_by_week
                                  or value > acwr_by_week[week]):
            acwr_by_week[week] = value
        if row["gap_warmup"]:
            gap_weeks.add(week)
    return acwr_by_week, gap_weeks


def _koi_overlay(
    conn: sqlite3.Connection,
) -> tuple[dict[str, set[str]], dict[str, set[str]],
           dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """Тяжёлые недели и полосы из ``v_weekly_koi`` (по группе и упражнению)."""
    heavy_by_group: dict[str, set[str]] = defaultdict(set)
    heavy_by_exercise: dict[str, set[str]] = defaultdict(set)
    band_by_group: dict[tuple[str, str], str] = {}
    band_by_exercise: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT week, exercise, exercise_group, band FROM v_weekly_koi"
    ):
        week = row["week"]
        exercise = row["exercise"]
        group = row["exercise_group"]
        band = row["band"]
        if band is None:
            continue
        for store, key in (
            (band_by_group, (week, group)),
            (band_by_exercise, (week, exercise)),
        ):
            current = store.get(key)
            if current is None or _BAND_RANK.get(band, -1) > _BAND_RANK.get(current, -1):
                store[key] = band
        if band in HEAVY_BANDS:
            heavy_by_group[group].add(week)
            heavy_by_exercise[exercise].add(week)
    return (
        dict(heavy_by_group),
        dict(heavy_by_exercise),
        band_by_group,
        band_by_exercise,
    )


@dataclass(frozen=True, slots=True)
class WeekPoint:
    """Одна неделя дуги: относительная работа и её оверлей."""

    week: str
    surface: str
    tonnage: float
    bodyweight: float
    r: float
    delta: float | None
    after_gap: bool
    acwr: float | None
    acwr_stop: bool
    koi_band: str | None
    koi_stop: bool
    stop: bool


def arc(
    conn: sqlite3.Connection,
    *,
    group: str | None = None,
    exercise: str | None = None,
) -> list[WeekPoint]:
    """Дуга относительной работы поверхности: ``R`` и ``ΔR`` по ISO-неделям.

    ``group`` — группа таксономии (по умолчанию поверхность — группа);
    ``exercise`` — конкретное упражнение (селектор, R3-D). Оба сразу —
    ``ValueError``. Пустая поверхность (нет строк) даёт ``[]``.

    Дельта считается к предыдущей неделе **с данными** этой поверхности; первая
    неделя без дельты. Разрывы не шьются — ``after_gap`` только аннотирует, что
    неделя стоит в окне после простоя (``load_daily.gap_warmup``).
    """
    if group is not None and exercise is not None:
        raise ValueError("поверхность: либо group, либо exercise, не оба")
    if group is None and exercise is None:
        return []
    if exercise is not None:
        surface, kind = exercise, "exercise"
    else:
        surface, kind = group, "group"

    tonnage = _tonnage(_pool(conn), group=group, exercise=exercise)
    bodyweight = estimate_bodyweight(conn)
    acwr_by_week, gap_weeks = _acwr_overlay(conn)
    heavy_by_group, heavy_by_exercise, band_by_group, band_by_exercise = \
        _koi_overlay(conn)

    points: list[WeekPoint] = []
    prev_r: float | None = None
    for week in sorted(tonnage):
        total = tonnage[week]
        bw = bodyweight.get(int(week[:4]), BODY_WEIGHT)
        r = total / bw
        delta = (r - prev_r) if prev_r is not None else None
        acwr = acwr_by_week.get(week)
        acwr_stop = acwr is not None and acwr > ACWR_ALERT
        if kind == "group":
            koi_stop = week in heavy_by_group.get(surface, ())
            koi_band = band_by_group.get((week, surface))
        else:
            koi_stop = week in heavy_by_exercise.get(surface, ())
            koi_band = band_by_exercise.get((week, surface))
        points.append(WeekPoint(
            week=week,
            surface=surface,
            tonnage=total,
            bodyweight=bw,
            r=r,
            delta=delta,
            after_gap=week in gap_weeks,
            acwr=acwr,
            acwr_stop=acwr_stop,
            koi_band=koi_band,
            koi_stop=koi_stop,
            stop=acwr_stop or koi_stop,
        ))
        prev_r = r
    return points


def _stops(point: WeekPoint) -> str:
    parts: list[str] = []
    if point.acwr_stop:
        parts.append("ACWR")
    if point.koi_stop:
        parts.append("КОИ")
    return " + ".join(parts)


def format_arc(points: list[WeekPoint]) -> str:
    """Таблица дуги: неделя | R | Δ R | ACWR | КОИ | разрыв | стоп.

    Детерминирована (золотой пин): числа — фиксированная точность, ``—`` для
    отсутствующего (первая неделя без дельты, нет ACWR/KOI). Пусто — «нет
    данных».
    """
    if not points:
        return "нет данных\n"
    surface = points[0].surface
    lines = [f"относительная работа недели: {surface} ({len(points)} недель)"]
    lines.append(
        "неделя      R      Δ R     ACWR     КОИ             разрыв   стоп"
    )
    lines.append("-" * 70)
    for point in points:
        r = f"{point.r:>7.1f}"
        delta = "—".rjust(7) if point.delta is None else f"{point.delta:>+7.1f}"
        acwr = "—".rjust(6) if point.acwr is None else f"{point.acwr:>6.2f}"
        koi = (point.koi_band or "—").ljust(15)
        gap = "разрыв" if point.after_gap else ""
        lines.append(
            f"{point.week:<10} {r} {delta} {acwr}   {koi} {gap:<8} "
            f"{_stops(point)}".rstrip()
        )
    return "\n".join(lines) + "\n"