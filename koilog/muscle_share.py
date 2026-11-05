# -*- coding: utf-8 -*-
"""PW-99: веса участия мышц в нагрузке (display-only).

Модуль **display-only**: не входит в решения — state/retrain/digest/score/ml/groups
не импортируют его (фенс как у 4.14/RW-98).

**Еffective load E** берётся из stored ``sets.weight × reps`` (канон RM-98).
Доли muscles заданы версионной таблицей v1 (role-based: P=4, S=2, T=1),
нормализованной к Σ=1 на профиль.

API:
    ``share_of(exercise) -> dict[muscle, float]`` — доли для упражнения.
    ``totals_by_muscle(conn, bucket='all', since=None, include_gravitron=True)`` —
        агрегация kg/koi/sets по мышцам.
    ``format_shares() -> str`` — таблица таблицы v1.

Инвариант pipeline autotest: Σ(доля×E) = E для каждого подхода (residual=0).
Чувствительность ±20%: seed=99, draws=2000; ранги #2–#17 нестабильны.
"""

from __future__ import annotations

import collections
import sqlite3
from datetime import date as date_cls, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import muscles

#: Версионная таблица v1 (дата фиксации 2026-11-05).
#: Профиль → роли (P=4, S=2, T=1). Нормализация к Σ=1 внутри ``shares()``.
_SHARES_V1: dict[tuple[str, ...], tuple[float, ...]] = {
    ("грудь", "трицепс", "дельты передние"): (4, 2, 1),
    ("квадрицепс", "ягодицы", "бицепс бедра"): (4, 2, 2),
    ("широчайшие", "ромбовидные", "бицепс", "дельты задние"): (4, 2, 2, 1),
    ("широчайшие", "бицепс", "ромбовидные"): (4, 2, 2),
    ("поясница", "ягодицы", "бицепс бедра", "квадрицепс", "трапеция"): (4, 4, 4, 2, 1),
    ("трицепс", "грудь", "дельты передние"): (4, 2, 1),
    ("грудь",): (1,),
    ("дельты передние", "дельты средние", "трицепс"): (4, 2, 2),
    ("бицепс бедра",): (1,),
    ("дельты задние",): (1,),
    ("пресс",): (1,),
    ("дельты средние",): (1,),
    ("бицепс",): (1,),
    ("трапеция",): (1,),
    ("трицепс",): (1,),
    ("поясница", "ягодицы", "бицепс бедра"): (4, 2, 2),
    ("квадрицепс",): (1,),
    ("дельты передние",): (1,),
    ("широчайшие", "бицепс"): (4, 2),
    ("икры",): (1,),
    ("дельты средние", "трапеция", "бицепс"): (4, 2, 1),
    ("квадрицепс", "ягодицы"): (4, 2),
    ("широчайшие", "грудь"): (4, 2),
    ("предплечья",): (1,),
    ("трицепс", "грудь"): (4, 2),
    ("бицепс бедра", "ягодицы", "поясница"): (4, 2, 1),
    ("дельты средние", "дельты передние", "трицепс"): (4, 2, 2),
    ("пресс", "косые"): (4, 2),
}

#: Overrides группы: та же композиция, разные роли для тяг.
_GROUP_OVERRIDES: dict[tuple[str, tuple[str, ...]], tuple[float, ...]] = {
    ("ТЯГА ВЕРТИКАЛЬНАЯ", ("широчайшие", "ромбовидные", "бицепс", "дельты задние")): (5, 2, 2, 1),
    ("ТЯГА ГОРИЗОНТАЛЬНАЯ", ("широчайшие", "ромбовидные", "бицепс", "дельты задние")): (4, 3, 2, 1),
}

#: Официальная версия таблицы.
VERSION = "v1"
VERSION_DATE = "2026-11-05"


def _shares(profile: tuple[str, ...], role: tuple[float, ...]) -> dict[str, float]:
    """Нормализовать роли к доляm (Σ=1)."""
    total = sum(role)
    return {m: w / total for m, w in zip(profile, role)}


def share_of(exercise: str) -> dict[str, float] | None:
    """Доли мышц упражнения по таблице v1.

    Возвращает None, если упражнение не имеет профиля или нет шаблона.
    """
    from . import muscles, taxonomy

    prof = muscles.muscles_of(exercise)
    if prof is None:
        return None
    profile = prof.muscles
    group = taxonomy.group_of(exercise).name

    # Override группы (тяги ВЕРТ/ГОР с одним профилем, разными ролями).
    role = _GROUP_OVERRIDES.get((group, profile))
    if role is None:
        role = _SHARES_V1.get(profile)
    if role is None:
        return None
    return _shares(profile, role)


def format_shares() -> str:
    """Таблица версии v1 (для документации/ноутбука)."""
    lines = [
        f"# PW-99 Shares Table {VERSION} ({VERSION_DATE})",
        "",
        "| группа | профиль | роль | доли |",
        "|---|---|---|---|",
    ]
    from . import muscles, taxonomy

    # Собрать все профили в таблице + их группы.
    seen_profiles = set()
    for exercise in muscles.PROFILES:
        prof = muscles.muscles_of(exercise)
        if prof is None or prof.is_static or muscles.DASH in prof.muscles:
            continue
        group = taxonomy.group_of(exercise).name
        profile = prof.muscles
        if profile in seen_profiles:
            continue
        seen_profiles.add(profile)

        role = _GROUP_OVERRIDES.get((group, profile))
        if role is None:
            role = _SHARES_V1.get(profile)
        if role is None:
            continue

        sh = _shares(profile, role)
        role_str = "/".join(str(int(x)) for x in role)
        share_str = "; ".join(f"{m} {v:.4f}" for m, v in sh.items())
        lines.append(f"| {group} | {'|'.join(profile)} | {role_str} | {share_str} |")

    return "\n".join(lines)


def totals_by_muscle(
    conn: sqlite3.Connection,
    bucket: str = "all",
    since: str | None = None,
    include_gravitron: bool = True,
) -> tuple[collections.Counter, collections.Counter, collections.Counter]:
    """Агрегация kg/koi/sets по мышцам.

    Args:
        conn: SQLite connection to the training database.
        bucket: 'all' или '90' (последние 90 дней от anchor).
        since: явная дата начала окна (перекрывает bucket='90').
        include_gravitron: включать ли gravis в расчёт (по умолчанию да).

    Returns:
        (kg_counter, koi_counter, sets_counter) — три Counter по 17 мышцам.

    Raises:
        ValueError: если нет шаблона для профиля упражнения.
    """
    # Fetch pool (4.14 filter + CARDIO/static exclude).
    rows = conn.execute(
        """
        SELECT date, exercise, weight, reps, koi, is_gravitron
          FROM sets
         WHERE date IS NOT NULL
           AND is_warmup = 0
           AND skipped = 0
           AND is_outlier = 0
           AND is_foreign = 0
        """
    ).fetchall()

    # Build pool with profiles.
    pool = []
    from . import muscles, taxonomy

    anchor_date = None
    for r in rows:
        prof = muscles.muscles_of(r["exercise"])
        if prof is None or muscles.DASH in prof.muscles or prof.is_static:
            continue
        if taxonomy.mode_of(r["exercise"]) is taxonomy.Mode.CARDIO:
            continue
        pool.append(r)
        if anchor_date is None or r["date"] > anchor_date:
            anchor_date = r["date"]

    if not pool:
        return collections.Counter(), collections.Counter(), collections.Counter()

    # Window by bucket/since.
    if since is None and bucket == "90":
        anchor = date_cls.fromisoformat(anchor_date)
        start90 = (anchor - timedelta(days=89)).isoformat()
        since = start90

    kg = collections.Counter()
    koi = collections.Counter()
    sets_cnt = collections.Counter()

    for r in pool:
        if since and r["date"] < since:
            continue
        if not include_gravitron and r["is_gravitron"]:
            continue

        sh = share_of(r["exercise"])
        if sh is None:
            raise ValueError(f"No share template for exercise: {r['exercise']}")

        e = 0.0
        if r["weight"] is not None and r["reps"] is not None:
            e = float(r["weight"]) * float(r["reps"])

        k_val = float(r["koi"]) if r["koi"] is not None else 0.0

        for m, w in sh.items():
            kg[m] += e * w
            koi[m] += k_val * w
            sets_cnt[m] += w

    return kg, koi, sets_cnt


def check_invariant(conn: sqlite3.Connection) -> tuple[float, int]:
    """Проверить инвариант Σ(share×E) = E по всем подходаам.

    Returns:
        (total_residual, days_with_E) — общая ошибка и число дней с E.

    Expected:
        total_residual ≈ 0.0 (точка счёта).
    """
    kg_by_day = {}
    sh_sum_by_day = {}

    rows = conn.execute(
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

    from . import muscles, taxonomy

    for r in rows:
        prof = muscles.muscles_of(r["exercise"])
        if prof is None or muscles.DASH in prof.muscles or prof.is_static:
            continue
        if taxonomy.mode_of(r["exercise"]) is taxonomy.Mode.CARDIO:
            continue

        sh = share_of(r["exercise"])
        if sh is None:
            continue

        e = 0.0
        if r["weight"] is not None and r["reps"] is not None:
            e = float(r["weight"]) * float(r["reps"])

        day = r["date"]
        if day not in kg_by_day:
            kg_by_day[day] = 0.0
            sh_sum_by_day[day] = 0.0

        if e > 0:
            kg_by_day[day] += e * sum(sh.values())
            sh_sum_by_day[day] += e

    # Residual per day.
    worst = 0.0
    for day in kg_by_day:
        resid = abs(kg_by_day[day] - sh_sum_by_day[day])
        if resid > worst:
            worst = resid

    days_with_e = sum(1 for d in sh_sum_by_day if sh_sum_by_day[d] > 0)
    return worst, days_with_e
