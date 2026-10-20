"""State-машина возврата: «перерыв → рампа → штатный» (раунд 5+5a).

Пререгистрация — ``docs/state-machine-spec.md`` (решения O1–O3 = **A/A/A**).
Смысл модуля: решение дня («что сегодня») собирается **в одном месте** и
опирается только на данные, а не на историю запусков. Дайджест
(:mod:`koilog.health.digest`) не собирает решение из условий сам - он рендерит
готовое состояние.

Состояния (приоритет ``sick > rest > ramp > normal``):

- ``sick`` - активный эпизод или красный флаг недовосстановления: по плану не
  тренироваться; счётчик рампы **сбрасывается** (рампа начнётся заново после
  эпизода, а не «на второй день» двух болезней подряд);
- ``rest`` - гейт ВСР/RHR не пройден **не** по окну тишины (каналы держат):
  покой; рампа **приостанавливается, а не сбрасывается** (счётчик ``k``
  сохраняется, дни ``rest`` его не увеличивают);
- ``ramp`` - окно тишины после эпизода
  (:attr:`koilog.health.retrain.BaselineGate.window_only`) ``или`` длинный
  простой, при котором штатная нагрузка уже не «штатная»: возврат ступенями
  по :func:`koilog.health.retrain.return_plan`;
- ``normal`` - гейт готов и простоя/окна нет: штатный план.

Ключевая семантика (спека §5.1): ``k`` - число **выполненных** дней рампы
(день с тренировкой в журнале ``sets``), а не календарных. День ``rest`` или
пропуск ``k`` не увеличивает; переход ``ramp → normal`` требует прожитых
``RAMP_DAYS`` **выполненных** дней. Состояние дня - детерминированная функция
данных: повторный расчёт того же дня даёт то же состояние.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls

from . import illness as health_illness
from . import load as health_load
from . import retrain as health_retrain

__all__ = [
    "RAMP_DAYS",
    "RAMP_GAP_DAYS",
    "SPEC_VERSION",
    "STATE_SICK",
    "STATE_REST",
    "STATE_RAMP",
    "STATE_NORMAL",
    "RampTarget",
    "StateDay",
    "decide",
    "ramp_target",
    "state_day",
]

#: Версия рецепта состояния. Смена семантики (длины рампы, порогов) - новая
#: версия, чтобы числа разных версий не сравнивались между собой.
SPEC_VERSION = "state-v1"

#: Длина рампы в **выполненных** днях (спека §4, решение O1 = A). Окно тишины
#: 14 дн - это допуск к нагрузке, а не сама рампа: 5 дней - минимальная рампа,
#: которая уже не «два дня и по плану». Длительность болезни уже закодирована
#: окном тишины, рампа не кодирует тот же сигнал второй раз.
RAMP_DAYS = 5

#: Простой (дней без тренировок), после которого штатный план уже не «штатный»
#: (совпадает с горизонтом ``H_ONSET`` - тот же масштаб, что у ML-ламбды).
RAMP_GAP_DAYS = 7

STATE_SICK = "sick"
STATE_REST = "rest"
STATE_RAMP = "ramp"
STATE_NORMAL = "normal"


@dataclass(frozen=True, slots=True)
class RampTarget:
    """Целевой вес рампы: floor от 1RM до болезни (решение O2 = A).

    ``rule`` - **структурное поле плана** (``"ramp_floor"``): округление вниз
    к ближайшему доступному весу, а не ``next_weight`` (который даёт вверх).
    Тест прибивает: 1RM ⇒ 42 ⇒ 40 из стека. ``None`` веса - «без 1RM, по
    прошлой сессии» (рампа всё равно строится :func:`retrain.return_plan`).
    """

    weight: float | None
    reps: int | None
    rule: str
    base_weight: float | None = None
    base_rm: float | None = None
    base_date: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "weight": self.weight,
            "reps": self.reps,
            "rule": self.rule,
            "base_weight": self.base_weight,
            "base_rm": self.base_rm,
            "base_date": self.base_date,
            "reason": self.reason,
        }


@dataclass(slots=True)
class StateDay:
    """Состояние дня: что сегодня и почему (единственный источник правды)."""

    day: str
    state: str
    reason: str
    ramp_day: int = 0
    ramp_days: int = RAMP_DAYS
    ramp_paused: bool = False
    ramp_target: RampTarget | None = None
    since_training: int | None = None
    flag: str | None = None
    spec_version: str = SPEC_VERSION

    @property
    def on_ramp(self) -> bool:
        return self.state == STATE_RAMP

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "state": self.state,
            "reason": self.reason,
            "ramp_day": self.ramp_day,
            "ramp_days": self.ramp_days,
            "ramp_paused": self.ramp_paused,
            "ramp_target": (self.ramp_target.to_dict()
                            if self.ramp_target is not None else None),
            "since_training": self.since_training,
            "flag": self.flag,
            "spec_version": self.spec_version,
        }


# --------------------------------------------------------------------------
# Чтение данных
# --------------------------------------------------------------------------


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _training_days(conn: sqlite3.Connection) -> list[str]:
    """Дни с тренировкой (любое упражнение, рабочие подходы)."""
    try:
        rows = conn.execute(
            """SELECT DISTINCT date FROM sets
               WHERE rm_session IS NOT NULL AND is_warmup = 0 AND skipped = 0
               ORDER BY date"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [row[0] for row in rows]


def _last_episode_end(conn: sqlite3.Connection, day: str) -> str | None:
    """Дата окончания последнего завершённого эпизода не позже ``day``.

    ``gap_only`` пропускаются: это не болезнь, а длинный перерыв в журнале
    (см. :mod:`koilog.health.illness`). Активный эпизод сюда не попадает -
    его обрабатывает приоритет ``sick``.
    """
    if not _has_table(conn, "illness_episodes"):
        return None
    best: str | None = None
    for episode in health_illness.episodes(conn):
        if episode.level == "gap_only":
            continue
        if episode.end <= day and (best is None or episode.end > best):
            best = episode.end
    return best


def _since_training(conn: sqlite3.Connection, day: str) -> int | None:
    """Дней с последней тренировки не позже ``day`` (``None``, если их нет)."""
    last = None
    for trained in _training_days(conn):
        if trained <= day:
            last = trained
        else:
            break
    if last is None:
        return None
    return (date_cls.fromisoformat(day) - date_cls.fromisoformat(last)).days


def _ramp_completed(conn: sqlite3.Connection, day: str,
                    anchor: str | None) -> int:
    """Число **выполненных** дней рампы к ``day`` (``k``).

    ``anchor`` - дата, с которой идёт отсчёт (конец эпизода или начало
    простоя). Считаются только дни с тренировкой в интервале
    ``(anchor, day]``: день ``rest``/пропуск счётчик не двигает (спека §5.1).
    Если ``anchor`` не задан, считается 0 (рампу не от чего отсчитывать).
    """
    if anchor is None:
        return 0
    return sum(1 for trained in _training_days(conn)
               if anchor < trained <= day)


def _last_training_before(conn: sqlite3.Connection, day: str) -> str | None:
    last = None
    for trained in _training_days(conn):
        if trained < day:
            last = trained
        else:
            break
    return last


# --------------------------------------------------------------------------
# Целевой вес рампы (5a)
# --------------------------------------------------------------------------


def ramp_target(
    conn: sqlite3.Connection,
    day: str,
    exercise: str | None = None,
    *,
    episode=None,
) -> RampTarget | None:
    """Целевой вес рампы: floor от 1RM до болезни (решение O2 = A).

    Округление - **вниз**: из лестницы инвентаря берётся ближайший вес **не
    тяжелее** «положенного» (:func:`koilog.core.snap_to_ladder`), а не
    ``next_weight`` (тот даёт вверх/ближайший - отклонённый вариант B).
    Пометка живёт структурным полем ``rule="ramp_floor"``, дайджест рендерит
    её механически.

    База - последняя тренировка упражнения **до начала эпизода** (или до дня):
    возвращаться надо к своему весу, а не к чужому. Если 1RM/веса нет -
    ``None`` (дайджест скажет «без 1RM, по прошлой сессии»).
    """
    try:
        plan = health_retrain.return_plan(
            conn, exercise, episode=episode, today=day)
    except sqlite3.OperationalError:
        return None
    if plan.start_weight is None:
        return RampTarget(
            weight=None, reps=None, rule=plan.rule,
            base_weight=plan.base_weight, base_rm=plan.base_rm,
            base_date=plan.base_date,
            reason=plan.reason or "нет предболезненного веса",
        )
    return RampTarget(
        weight=plan.start_weight,
        reps=plan.start_reps,
        rule=plan.rule,
        base_weight=plan.base_weight,
        base_rm=plan.base_rm,
        base_date=plan.base_date,
    )


# --------------------------------------------------------------------------
# Решение
# --------------------------------------------------------------------------


def _red_flag(load: dict, bad) -> bool:
    """Красный флаг недовосстановления.

    Порог монотонности берётся из общего источника
    (:attr:`koilog.health.load.MONOTONY_ALERT`), а не магическим числом:
    регресс силы при однообразной нагрузке и просевших ВСР/сне - это
    недовосстановление (Meeusen 2013).
    """
    monotony = load.get("monotony") or 0.0
    return bool(load.get("rm_regress") and bad("hrv_sdnn")
                and bad("sleep_hours")
                and monotony > health_load.MONOTONY_ALERT)


def decide(
    *,
    day: str,
    channels_list: list,
    load: dict,
    gate,
) -> StateDay:
    """Определить состояние дня из каналов, нагрузки и гейта.

    Это **единственное** место, где решение выбирается из условий: и
    :func:`state_day` (с базой), и :func:`digest.recommend` (совместимость с
    прямыми вызовами) идут сюда. Приоритет ``sick > rest > ramp > normal``.
    """
    by_key = {c.key: c for c in channels_list} if channels_list else {}

    def bad(key: str) -> bool:
        channel = by_key.get(key)
        return bool(channel and channel.bad)

    no_watch = not channels_list
    days_since = load.get("days_since_training")
    window_only = bool(gate is not None and getattr(gate, "window_only", False))
    ready = bool(gate is not None and getattr(gate, "ready", False))
    gate_reasons = tuple(getattr(gate, "reasons", ()) or ())
    # Именно активный эпизод, а не «окно тишины после эпизода»: последнее -
    # это ramp, а не sick (проверка по подстроке «эпизод» ловила бы оба).
    active_episode = any(reason.startswith("активный эпизод")
                         for reason in gate_reasons)

    # 1. sick: красный флаг или активный эпизод.
    if _red_flag(load, bad):
        return StateDay(day=day, state=STATE_SICK,
                        reason="красный флаг недовосстановления")
    if active_episode:
        return StateDay(day=day, state=STATE_SICK,
                        reason="активный эпизод болезни")

    # 2. rest: гейт не пройден и это не окно тишины.
    if gate is not None and not ready and not no_watch and not window_only:
        return StateDay(day=day, state=STATE_REST, ramp_paused=True,
                        reason="гейт ВСР/RHR держит")

    # 3. ramp: окно тишины ИЛИ длинный простой.
    gap = bool(load.get("gap_warmup")) or (
        days_since is not None and days_since >= RAMP_GAP_DAYS)
    if window_only:
        return StateDay(day=day, state=STATE_RAMP,
                        reason="окно тишины после эпизода")
    if gap:
        return StateDay(day=day, state=STATE_RAMP,
                        reason="возврат после простоя")

    # 4. normal.
    state_day = StateDay(day=day, state=STATE_NORMAL, reason="штатный план")
    # «Завтра легко»: ЧДД + RHR + ВСР ушли в «плохую» сторону вместе -
    # прототип сигнатуры начинающейся инфекции. Это под-вариант normal:
    # состояние не sickness, но план корректируется.
    if bad("resp_rate") and bad("rhr") and bad("hrv_sdnn"):
        state_day.flag = "easy"
    return state_day


def state_day(
    conn: sqlite3.Connection,
    day: str | None = None,
    *,
    exercise: str | None = None,
) -> StateDay:
    """Состояние дня из базы (единственный источник правды для дайджеста).

    Данные кончились (нет ``aw_daily``/``load_daily`` вовсе - чистый клон):
    состояние ``normal`` с причиной «нет данных», рампа не выдумывается из
    пустоты. Первый день истории тоже ``normal``: рампа - про **возврат**, а
    не про старт.
    """
    from . import digest as health_digest   # локально: избегаем кольца импорта

    channels_list = health_digest.channels(conn, day)
    load = health_digest.load_state(conn, day)
    resolved = day
    if resolved is None:
        if channels_list:
            resolved = health_digest._query(conn, health_digest._SELECT)[-1][0]
        elif load.get("available"):
            resolved = load["date"]

    if resolved is None:
        return StateDay(day="?", state=STATE_NORMAL, reason="нет данных")

    # Данных часов/нагрузки нет вовсе (чистый клон): не выдумываем рампа-цель
    # и не строим гейт - состояние «штатный», причина «нет данных».
    if not channels_list and not load.get("available"):
        return StateDay(day=resolved, state=STATE_NORMAL, reason="нет данных")

    gate = health_retrain.baseline_gate(conn, resolved)
    base = decide(day=resolved, channels_list=channels_list, load=load,
                  gate=gate)

    base.since_training = _since_training(conn, resolved)

    if base.state == STATE_RAMP:
        # Отсчёт рампы зависит от причины (§5.2). Рампа после окна тишины
        # считается от конца эпизода: дни болезни в счёт не идут. Рампа из-за
        # простоя - от последней тренировки: длинный перерыв, а не болезнь.
        if "просто" in base.reason:
            anchor = _last_training_before(conn, resolved)
        else:
            anchor = _last_episode_end(conn, resolved)
            if anchor is None:
                anchor = _last_training_before(conn, resolved)
        base.ramp_day = _ramp_completed(conn, resolved, anchor)
        base.ramp_days = RAMP_DAYS
        # Выход §3: рампа прожита (k >= RAMP_DAYS **выполненных** дней) и гейт
        # готов -> нормальный план. Простой (k не растёт без тренировок) сам
        # держит рампа открытой, поэтому отдельного условия не нужно.
        if base.ramp_day >= RAMP_DAYS and gate.ready:
            return StateDay(
                day=resolved, state=STATE_NORMAL,
                reason=f"рампа прожита ({base.ramp_day} из {RAMP_DAYS})",
                ramp_day=base.ramp_day, ramp_days=RAMP_DAYS,
                since_training=base.since_training)
        base.ramp_target = ramp_target(conn, resolved, exercise)
    elif base.state == STATE_REST:
        # Пауза: счётчик сохраняется (число не показываем), но рампа идёт.
        anchor = _last_episode_end(conn, resolved)
        if anchor is not None:
            base.ramp_day = _ramp_completed(conn, resolved, anchor)
            base.ramp_days = RAMP_DAYS

    return base
