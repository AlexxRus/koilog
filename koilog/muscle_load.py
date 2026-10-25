"""Нагрузка и свежесть по мышцам (4.14) — display-only.

Пререг: ``docs/4-14-prereg.md`` (принят 2026-10-25). Две валюты:

* **кольцо** — подходы с равным делением ``1/N`` на мышцы упражнения; одно
  число на мышцу, оба режима, кросс-режимная агрегация подходов разрешена
  пререгом;
* **тоннаж** — только внутри режима (``iron`` / ``bodyweight``); кросс-режимно
  не суммируется никогда (канон 4.2/4.5).

Пул: все сеты, кроме разминки, «—», кардио (группа КАРДИО целиком), статики,
``skipped`` и выбросов. Требование «есть вес и повторы» к пулу не применяется:
подход без веса — стимул и доля кольца, но его тоннаж 0.

Модуль не входит в решения (state/gate/ramp/digest.recommend/ml_*) — фенс
display-only, проверяется import-фенсом в тестах.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls

from . import muscles, taxonomy

__all__ = [
    "BODYWEIGHT",
    "IRON",
    "RECOVERY_DAYS",
    "Freshness",
    "MuscleFreshness",
    "MuscleLoad",
    "MuscleTonnage",
    "Ring",
    "Tonnage",
    "freshness",
    "ring",
    "session_ring",
    "tonnage_by_muscle",
    "week_key",
    "week_ring",
]

#: Нормализованные имена режимов (в ``sets.weight_mode`` — ``absolute`` /
#: ``vest_only`` / ``total_bodyweight``; ``absolute`` = железо).
IRON = "iron"
BODYWEIGHT = "bodyweight"

#: Порог «восстанавливается»: 48 ч при дневной дискретизации = дней <= 1.
RECOVERY_DAYS = 1

#: Сколько меток ожидается в кольце и свежести (закрытый список 4.10).
_LABELS = muscles.MUSCLES


@dataclass(frozen=True, slots=True)
class MuscleLoad:
    """Вклад мышцы в кольцо: сырые подходы, доля ``1/N`` и её доля пула."""

    muscle: str
    sets: int
    units: float
    share: float


@dataclass(frozen=True, slots=True)
class Ring:
    """Кольцо периода: число подходов пула и 17 меток (включая нули)."""

    sets: int
    loads: tuple[MuscleLoad, ...]

    def of(self, muscle: str) -> MuscleLoad:
        for load in self.loads:
            if load.muscle == muscle:
                return load
        raise KeyError(muscle)


@dataclass(frozen=True, slots=True)
class MuscleTonnage:
    """Тоннаж мышцы внутри одного режима: кг, доля режима и сырые подходы."""

    muscle: str
    sets: int
    kg: float
    share: float


@dataclass(frozen=True, slots=True)
class Tonnage:
    """Тоннаж режима; сумма долей = 1 внутри режима (или 0 при пустоте)."""

    mode: str
    sets: int
    total_kg: float
    loads: tuple[MuscleTonnage, ...]

    def of(self, muscle: str) -> MuscleTonnage:
        for load in self.loads:
            if load.muscle == muscle:
                return load
        raise KeyError(muscle)


@dataclass(frozen=True, slots=True)
class MuscleFreshness:
    """Свежесть мышцы: последний стимул, дни и статус по порогу 48 ч."""

    muscle: str
    last: str | None
    days: int | None
    sets: int

    @property
    def recovered(self) -> bool | None:
        """``None`` — стимула не было; иначе ``дней > RECOVERY_DAYS``."""
        if self.days is None:
            return None
        return self.days > RECOVERY_DAYS


@dataclass(frozen=True, slots=True)
class Freshness:
    """Свежесть на якорь: ``anchor`` = ``MAX(date)`` базы (или заданный день)."""

    anchor: str
    muscles: tuple[MuscleFreshness, ...]

    def of(self, muscle: str) -> MuscleFreshness:
        for row in self.muscles:
            if row.muscle == muscle:
                return row
        raise KeyError(muscle)


def week_key(day: str) -> str:
    """Ключ недели в формате проекта: ``strftime('%Y-W%W', date)``."""
    return date_cls.fromisoformat(day).strftime("%Y-W%W")


def _mode(exercise: str, stored: str | None) -> str:
    """Режим тоннажа по **записи** веса (пререг §3), не по ``mode_of``."""
    if stored:
        return IRON if stored == "absolute" else BODYWEIGHT
    return (IRON if taxonomy.weight_mode_of(exercise) is taxonomy.WeightMode.ABSOLUTE
            else BODYWEIGHT)


def _pool(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
    session_id: str | None = None,
    week: str | None = None,
) -> list[sqlite3.Row]:
    """Подходы пула за период/тренировку/неделю (границы включительно)."""
    rows = conn.execute(
        """
        SELECT date, session_id, exercise, weight, reps, weight_mode
          FROM sets
         WHERE date IS NOT NULL
           AND is_warmup = 0
           AND skipped = 0
           AND is_outlier = 0
         ORDER BY date, session_id
        """
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in rows:
        profile = muscles.muscles_of(row["exercise"])
        if profile is None:
            continue
        if muscles.DASH in profile.muscles or profile.is_static:
            continue
        if taxonomy.mode_of(row["exercise"]) is taxonomy.Mode.CARDIO:
            continue
        if since is not None and row["date"] < since:
            continue
        if until is not None and row["date"] > until:
            continue
        if session_id is not None and row["session_id"] != session_id:
            continue
        if week is not None and week_key(row["date"]) != week:
            continue
        out.append(row)
    return out


def _ring(rows: list[sqlite3.Row]) -> Ring:
    units: dict[str, float] = {muscle: 0.0 for muscle in _LABELS}
    counts: dict[str, int] = {muscle: 0 for muscle in _LABELS}
    for row in rows:
        profile = muscles.muscles_of(row["exercise"])
        share = 1.0 / len(profile.muscles)
        for muscle in profile.muscles:
            units[muscle] += share
            counts[muscle] += 1
    total = float(len(rows))
    loads = tuple(
        MuscleLoad(muscle, counts[muscle], units[muscle],
                   units[muscle] / total if total else 0.0)
        for muscle in _LABELS
    )
    return Ring(sets=len(rows), loads=loads)


def ring(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
) -> Ring:
    """Кольцо «нагрузка по мышцам» за интервал: подходы с делением 1/N."""
    return _ring(_pool(conn, since=since, until=until))


def session_ring(conn: sqlite3.Connection, session_id: str) -> Ring:
    """Кольцо одной тренировки (``session_id``)."""
    return _ring(_pool(conn, session_id=session_id))


def week_ring(conn: sqlite3.Connection, week: str) -> Ring:
    """Кольцо календарной недели ``YYYY-Www`` (``%W``, как в проекте)."""
    return _ring(_pool(conn, week=week))


def tonnage_by_muscle(
    conn: sqlite3.Connection,
    mode: str,
    since: str | None = None,
    until: str | None = None,
) -> Tonnage:
    """Тоннаж ``(мышца × режим)``; ``mode`` обязателен, кросс-суммы нет.

    ``ValueError`` на неизвестный режим — структурный запрет: смешать iron и
    bodyweight в одно число нельзя (пререг §1/§5).
    """
    if mode not in (IRON, BODYWEIGHT):
        raise ValueError(f"режим только {IRON!r} или {BODYWEIGHT!r}: {mode!r}")
    kg: dict[str, float] = {muscle: 0.0 for muscle in _LABELS}
    counts: dict[str, int] = {muscle: 0 for muscle in _LABELS}
    sets = 0
    for row in _pool(conn, since=since, until=until):
        if _mode(row["exercise"], row["weight_mode"]) != mode:
            continue
        if row["weight"] is None or row["reps"] is None:
            continue
        sets += 1
        total = float(row["weight"] * row["reps"])
        profile = muscles.muscles_of(row["exercise"])
        for muscle in profile.muscles:
            kg[muscle] += total / len(profile.muscles)
            counts[muscle] += 1
    total_kg = sum(kg.values())
    loads = tuple(
        MuscleTonnage(muscle, counts[muscle], kg[muscle],
                      kg[muscle] / total_kg if total_kg else 0.0)
        for muscle in _LABELS
    )
    return Tonnage(mode=mode, sets=sets, total_kg=total_kg, loads=loads)


def freshness(conn: sqlite3.Connection, day: str | None = None) -> Freshness:
    """Свежесть мышц на якорь.

    Якорь — ``day`` или ``MAX(date)`` **всей** базы (пререг §6): календарное
    «сегодня» не годится, иначе при пустых днях витрина ложно зеленеет.
    Стимул — любой подход пула, где мышца входит в разметку; ``1/N`` к датам
    не применяется.
    """
    if day is None:
        row = conn.execute("SELECT MAX(date) FROM sets").fetchone()
        if row is None or row[0] is None:
            return Freshness(anchor="", muscles=tuple(
                MuscleFreshness(muscle, None, None, 0) for muscle in _LABELS))
        anchor = row[0]
    else:
        anchor = day
    last: dict[str, str] = {}
    counts: dict[str, int] = {muscle: 0 for muscle in _LABELS}
    for row in _pool(conn, until=anchor):
        profile = muscles.muscles_of(row["exercise"])
        for muscle in profile.muscles:
            counts[muscle] += 1
            if muscle not in last or row["date"] > last[muscle]:
                last[muscle] = row["date"]
    anchor_day = date_cls.fromisoformat(anchor)
    rows = tuple(
        MuscleFreshness(
            muscle,
            last.get(muscle),
            (anchor_day - date_cls.fromisoformat(last[muscle])).days
            if muscle in last else None,
            counts[muscle],
        )
        for muscle in _LABELS
    )
    return Freshness(anchor=anchor, muscles=rows)
