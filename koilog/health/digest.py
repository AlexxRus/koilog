"""Дневной дайджест: одна рекомендация на сегодня.

Собирает в одну команду то, что раньше приходилось смотреть по частям:
z-каналы часов против личного фона, состояние нагрузки и **одну**
рекомендацию по правилам. Это не диагноз и не предсказание: у автора
эпизодов единицы, поэтому дайджест читает известные красные флаги
(Meeusen 2013), а не «предсказывает болезнь».

Правила решения живут **не здесь**, а в state-машине
(:func:`koilog.health.state.decide`): дайджест только рендерит готовое
состояние дня. Приоритет состояний ``sick > rest > ramp > normal``:

1. **sick** - красный флаг недовосстановления (регресс e1RM при высокой
   монотонности и падении ВСР/сна; Meeusen 2013) или активный эпизод:
   по плану не тренироваться;
2. **rest** - гейт ВСР/RHR не пройден (:func:`...retrain.baseline_gate`):
   покой, рампа приостановлена (счётчик сохраняется);
3. **ramp** - окно тишины после эпизода
   (:attr:`...retrain.BaselineGate.window_only`) или длинный простой:
   возврат ступенями по :mod:`koilog.health.retrain`;
4. **normal** - «завтра легко», если ЧДД, RHR и ВСР отклонились в «плохую»
   сторону одновременно (прототип сигнатуры болезни), иначе «можно по плану».

Канонический скор дня (:mod:`koilog.health.score`) решение **не
переопределяет**, а объясняет: после causal replay он тихо мониторит.

Модуль держится на стандартной библиотеке и ядре ``koilog``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls

from . import groups as health_groups
from . import illness as health_illness
from . import load as health_load
from . import retrain as health_retrain
from .text import table

__all__ = [
    "ACWR_ALERT",
    "CHANNELS",
    "GAP_DAYS",
    "MONOTONY_ALERT",
    "Z_CHANNEL",
    "Channel",
    "channels",
    "day_state",
    "format_digest",
    "load_state",
    "recommend",
    "spec_warning",
]

#: Окно личного фона для z-каналов (как в :mod:`koilog.health.illness`).
BASELINE_DAYS = health_illness.BASELINE_DAYS

#: z, выше которого канал считается отклонившимся (в «плохую» сторону).
Z_CHANNEL = 1.5

#: ACWR, выше которого нагрузка считается вздутой.
ACWR_ALERT = 1.5

#: Монотонность Foster: выше - однообразная нагрузка (общий источник с
#: :mod:`koilog.health.load`; решение по красному флагу - в
#: :func:`koilog.health.state.decide`).
MONOTONY_ALERT = health_load.MONOTONY_ALERT

#: Сколько дней без тренировки считать «разрывом серии».
GAP_DAYS = 14

#: Каналы часов: ``(позиция в выборке, ключ, подпись, знак «плохо»)``.
#: Плюс - плохо, когда растёт (пульс, ЧДД, температура); минус - когда
#: падает (ВСР, SpO2, сон).
CHANNELS: tuple[tuple[int, str, str, int], ...] = (
    (1, "rhr", "RHR", +1),
    (2, "hrv_sdnn", "ВСР", -1),
    (3, "resp_rate", "ЧДД", +1),
    (4, "wrist_temp", "t запястья", +1),
    (5, "spo2", "SpO2", -1),
    (6, "sleep_hours", "сон", -1),
)

_SELECT = (
    "SELECT date, rhr, hrv_sdnn, resp_rate, wrist_temp, spo2, sleep_hours "
    "FROM aw_daily ORDER BY date"
)


@dataclass(frozen=True, slots=True)
class Channel:
    """Один канал часов за день: значение, z к фону и признак отклонения."""

    key: str
    label: str
    value: float | None
    z: float | None
    bad: bool

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "z": self.z,
            "bad": self.bad,
        }


def _query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    """Выполнить запрос, вернув пустой список, если таблицы ещё нет."""
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def _index_upto(dates: list[str], today: str | None) -> int | None:
    """Индекс последнего дня не позже ``today`` (``None``, если такого нет)."""
    index = None
    for i, day in enumerate(dates):
        if today is None or day <= today:
            index = i
    return index


def channels(
    conn: sqlite3.Connection, today: str | None = None
) -> list[Channel]:
    """z-каналы часов на ``today`` (по умолчанию - последний день часов)."""
    rows = _query(conn, _SELECT)
    if not rows:
        return []
    dates = [row[0] for row in rows]
    index = _index_upto(dates, today)
    if index is None:
        return []

    out: list[Channel] = []
    for position, key, label, sign in CHANNELS:
        values = [row[position] for row in rows]
        z = health_illness.zscore(values, BASELINE_DAYS)[index]
        value = values[index]
        bad = z is not None and sign * z > Z_CHANNEL
        out.append(Channel(key, label, value, z, bad))
    return out


def load_state(
    conn: sqlite3.Connection, today: str | None = None
) -> dict:
    """Состояние нагрузки на ``today`` из ``load_daily``."""
    rows = _query(
        conn,
        "SELECT date, acwr, acwr_warmup, monotony, strain, weekly_tonnage, "
        "rm_regress, gap_warmup, since_gap, sets FROM load_daily ORDER BY date",
    )
    if not rows:
        return {"available": False}
    dates = [row[0] for row in rows]
    index = _index_upto(dates, today)
    if index is None:
        return {"available": False}

    row = rows[index]
    last_training = None
    for i in range(index, -1, -1):
        if rows[i][9]:
            last_training = rows[i][0]
            break
    days_since = None
    if last_training:
        days_since = (date_cls.fromisoformat(row[0])
                      - date_cls.fromisoformat(last_training)).days

    return {
        "available": True,
        "date": row[0],
        "acwr": row[1],
        "acwr_warmup": row[2],
        "monotony": row[3],
        "strain": row[4],
        "weekly_tonnage": row[5],
        "rm_regress": bool(row[6]),
        "gap_warmup": bool(row[7]),
        "since_gap": row[8],
        "last_training": last_training,
        "days_since_training": days_since,
    }


def _score_note(score: dict | None) -> str:
    """Хвост про скор в тексте рекомендации - тихий мониторинг (спека §5.6).

    После causal replay скор не решает: он не переопределяет состояние и не
    меняет интонацию вердикта, только сообщает, что модель видит. Число скора
    в тексте не дублируется - оно уже есть строкой ``скор (причинный)``.
    """
    if not score or score.get("score") is None:
        return ""
    if score.get("threshold") is None:
        tail = "порог ещё не накоплен"
    elif score.get("alert"):
        tail = f"скор дня выше порога бюджета ({score['threshold']:.2f})"
    else:
        tail = f"скор дня в мониторинге (порог {score['threshold']:.2f})"
    return f" {tail[0].upper()}{tail[1:]}."


def _recommend_from_state(state, score: dict | None = None) -> dict:
    """Рендер рекомендации по готовому состоянию (:mod:`...state`).

    Решение принято :func:`koilog.health.state.decide`; здесь только текст.
    Уровни карточки: ``sick`` - красный флаг даёт ``red``, эпизод - ``hold``;
    ``rest`` - ``hold``; ``ramp`` - ``ramp``; ``normal`` - ``ok``.
    """
    from . import state as health_state

    note = _score_note(score)
    if state.state == health_state.STATE_SICK:
        if "красный флаг" in state.reason:
            return {
                "level": "red",
                "title": "красный флаг",
                "text": "сила падает при однообразной нагрузке и просевших "
                        "ВСР/сне - это признаки недовосстановления "
                        "(Meeusen 2013): снизь нагрузку. При боли в груди, "
                        "перебоях, несоразмерной одышке или пресинкопе - не "
                        "тренируйся и обратись к врачу.",
            }
        return {
            "level": "hold",
            "title": "покой",
            "text": "активный эпизод болезни: держи покой, вернись по рампе "
                    "(`python -m koilog return`) после 24 ч без симптомов."
                    + note,
        }
    if state.state == health_state.STATE_REST:
        return {
            "level": "hold",
            "title": "покой",
            "text": "возврат к нагрузке закрыт гейтом ВСР/RHR: "
                    "фон вне нормы. Гейт смотрит на две ночи подряд и с "
                    "порогом ±1 (мягче, чем «откл.» в таблице каналов, там "
                    "±1.5), поэтому «в фоне» по каналам и «держит» по гейту - "
                    "не противоречие. С симптомами (температура, миалгия) не "
                    "тренируйся - риск вирусного миокардита."
                    + _ramp_progress(state) + note,
        }
    if state.state == health_state.STATE_RAMP:
        if "окно тишины" in state.reason:
            return {
                "level": "ramp",
                "title": "окно тишины после эпизода",
                "text": "каналы в фоне, но сразу после болезни фона мало: "
                        "высокая ВСР тут - отскок, а не «готов». Возврат по "
                        "рампе: `python -m koilog return`."
                        + _ramp_progress(state) + note,
            }
        return {
            "level": "ramp",
            "title": "после простоя",
            "text": "ACWR вздулся в окне после разрыва - это не перегруз. "
                    "Стартуй по рампе: `python -m koilog return`."
                    + _ramp_progress(state),
        }
    # normal: «завтра легко» - прототип сигнатуры по каналам.
    if getattr(state, "flag", None) == "easy":
        return {
            "level": "easy",
            "title": "завтра легко",
            "text": "сразу три канала (ЧДД вверх, RHR вверх, ВСР вниз) "
                    "отклонились - типичная сигнатура начинающейся инфекции. "
                    "Завтра лёгкая нагрузка, следи за температурой.",
        }
    return {
        "level": "ok",
        "title": "можно по плану",
        "text": "каналы в личном фоне, нагрузка без резких скачков - "
                "тренируйся по плану.",
    }


def _ramp_progress(state) -> str:
    """«день k из N» или «(рампа приостановлена)» - см. спека §5.3."""
    k = getattr(state, "ramp_day", 0) or 0
    is_ramp = getattr(state, "state", None) == "ramp"
    if getattr(state, "ramp_paused", False):
        # Пауза значима, только если рампа уже начата (иначе это покой без
        # рампы - метка «приостановлена» вводила бы в заблуждение).
        return " (рампа приостановлена)" if (k or is_ramp) else ""
    n = getattr(state, "ramp_days", 0) or 0
    return f" Рампа: день {k} из {n}." if k else ""


def recommend(
    channels_list: list[Channel], load: dict, gate, score: dict | None = None,
    state=None,
) -> dict:
    """Одна рекомендация по правилам (см. модуль). Возвращает словарь.

    Решение принимается **в одном месте** - :func:`koilog.health.state.decide`
    (state-машина «перерыв → рампа → штатный»); здесь рендерится текст.
    ``score`` - канонический скор дня из ledger'а (``{"score", "threshold",
    "alert"}``): он не переопределяет решение, а **объясняет** его. ``state``
    можно передать готовым (из :func:`day_state`), иначе он строится из
    каналов/нагрузки/гейта - прямые вызовы остаются рабочими.
    """
    from . import state as health_state

    if state is None:
        state = health_state.decide(
            day=load.get("date") or "?", channels_list=channels_list,
            load=load, gate=gate)
    return _recommend_from_state(state, score)


def _newest_spec(conn: sqlite3.Connection) -> str | None:
    """Спека самой свежей строки ledger'а: текущая рецептура по факту записи."""
    rows = _query(conn, "SELECT spec_hash FROM ml_score ORDER BY id DESC LIMIT 1")
    return rows[0][0] if rows else None


def _score_row(conn: sqlite3.Connection, day: str | None,
               spec: str | None = None) -> dict | None:
    """Канонический скор дня из ledger'а ``ml_score`` (:mod:`...score`).

    Канон - последняя строка дня **под текущей спекой** (N-2, вариант B).
    Строка, записанная по другой рецептуре, - винтаж: она невоспроизводима
    (счёт по другому рецепту) и ни в канон, ни в порог не входит. Поэтому
    день, у которого нет строки под текущей спекой, показывает ближайший
    **каноничный** день с его датой, а не число винтажа.

    Если строки за сам день нет (день внутри эпизода модель не оценивает) -
    берётся ближайшая прошлая: карточка тогда показывает последний честный
    скор с датой, а не пустоту. Здесь намеренно нет импорта ``score`` - иначе
    получилось бы кольцо (``score`` зовёт :func:`recommend`), а вся нужная
    информация лежит в таблице.
    """
    if day is None:
        return None
    canon = spec or _newest_spec(conn)
    if canon is None:
        return None
    rows = _query(
        conn,
        "SELECT s.day, s.score, s.threshold, s.alert, s.as_of, s.computed_at, "
        "s.spec_hash "
        "FROM ml_score s WHERE s.spec_hash = ? AND s.day <= ? "
        "AND s.score IS NOT NULL "
        "AND s.id = (SELECT MAX(id) FROM ml_score x WHERE x.day = s.day "
        "AND x.spec_hash = s.spec_hash) "
        "ORDER BY s.day DESC LIMIT 1",
        (canon, day),
    )
    if not rows:
        return None
    day_of, score, threshold, alert, as_of, computed_at, spec = rows[0]
    return {
        "day": day_of,
        "score": score,
        "threshold": threshold,
        "alert": alert,
        "as_of": as_of,
        "computed_at": computed_at,
        "spec_hash": spec,
    }


def _ledger_specs(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Дни под каждой спекой ledger'а (последняя строка на день+спека)."""
    return _query(
        conn,
        "SELECT spec_hash, COUNT(DISTINCT day) FROM ml_score s "
        "WHERE s.id = (SELECT MAX(id) FROM ml_score x WHERE x.day = s.day "
        "AND x.spec_hash = s.spec_hash) "
        "GROUP BY spec_hash ORDER BY 2 DESC, 1",
    )


def _orphan_days(conn: sqlite3.Connection, canon: str) -> int:
    """Дни ledger'а, для которых нет строки под канонической спекой."""
    rows = _query(
        conn,
        "SELECT COUNT(*) FROM (SELECT DISTINCT day FROM ml_score "
        "EXCEPT SELECT DISTINCT day FROM ml_score WHERE spec_hash = ?)",
        (canon,),
    )
    return rows[0][0] if rows else 0


def _orphan_reason(conn: sqlite3.Connection, canon: str) -> str:
    """Почему у этих дней нет канона: хвосты эпизодов или что-то другое.

    Дни внутри эпизода помечены ``at_risk = 0``, и текущий рецепт их не
    оценивает вовсе - ни одна пересевка их не накроет. Если все «без канона»
    именно такие, причина названа прямо; иначе - общая формулировка.
    """
    total = _orphan_days(conn, canon)
    if not total:
        return ""
    rows = _query(
        conn,
        "SELECT COUNT(*) FROM ml_features WHERE at_risk = 0 AND date IN ("
        "SELECT DISTINCT day FROM ml_score "
        "EXCEPT SELECT DISTINCT day FROM ml_score WHERE spec_hash = ?)",
        (canon,),
    )
    zero = rows[0][0] if rows else 0
    if zero == total:
        return ("хвосты эпизодов: at_risk = 0 - рецепт их не оценивает, "
                "replay неприменим")
    return "дни вне текущего рецепта"


def spec_warning(conn: sqlite3.Connection, current: str | None = None) -> str | None:
    """Состав ledger'а скора: канон, винтаж и дни без канона (N-2, вариант B).

    Канон - строки **текущей** спеки; строки других рецептур остаются
    читаемыми, но помечены винтаж и в канон и порог не входят. Сообщение
    информационное: факты (хэши спек, числа дней, причина отсутствия канона)
    и никакого совета «нужен полный replay» - для хвостов эпизодов он
    невыполним, их не оценивает ни одна рецептура.

    Проверка - чистым SQL, без импорта ``score``/``ml`` (иначе кольцо:
    ``score`` зовёт :func:`recommend`); ``current`` подставляет вызывающий.
    """
    specs = _ledger_specs(conn)
    if not specs:
        return None
    canon = current or _newest_spec(conn)
    if canon is None:
        return None
    days = dict(specs)
    canon_days = days.get(canon, 0)
    vintage = [(spec, count) for spec, count in specs if spec != canon]
    orphans = _orphan_days(conn, canon)
    if not vintage and not orphans:
        return None
    parts = [f"канон скора — спека {canon}"
             + (f" ({canon_days} дн)" if canon_days else " (строк нет)")]
    if vintage:
        parts.append("винтаж: " + ", ".join(f"{spec} ({count} дн)"
                                            for spec, count in vintage))
    if orphans:
        reason = _orphan_reason(conn, canon)
        parts.append(f"{orphans} дн без канона"
                     + (f" ({reason})" if reason else ""))
    return "; ".join(parts)


def day_state(conn: sqlite3.Connection, today: str | None = None,
              spec: str | None = None) -> dict:
    """Полное состояние дня: дата, каналы, нагрузка, гейт, скор, состояние.

    ``spec`` - текущий ``spec_hash`` рецептуры (подставляет вызывающий,
    например CLI): без него видно только смешение винтов внутри ряда.
    Состояние (``state``) приходит из :mod:`koilog.health.state` - это
    единственный источник правды, а :func:`recommend` только рендерит текст.
    """
    from . import state as health_state

    channels_list = channels(conn, today)
    load = load_state(conn, today)
    resolved = today
    if resolved is None:
        if channels_list:
            resolved = _query(conn, _SELECT)[-1][0]
        elif load.get("available"):
            resolved = load["date"]
    gate = health_retrain.baseline_gate(conn, resolved)
    score = _score_row(conn, resolved, spec)
    day = health_state.state_day(conn, resolved)
    recommendation = _recommend_from_state(day, score)
    return {
        "today": resolved,
        "channels": channels_list,
        "load": load,
        "gate": gate.to_dict(),
        "state": day.to_dict(),
        "score": score,
        "spec_warning": spec_warning(conn, spec),
        "groups": groups_state(conn, resolved),
        "recommendation": recommendation,
    }


def groups_state(conn: sqlite3.Connection, day: str | None = None) -> dict:
    """Погрупповая нагрузка для E-строки (E2/E4) — **только описательная**.

    Считает rolling-7 по каждой подветке **(группа × режим)** с ``n_days``.
    Решение дня это не меняет (D11-b=A): дайджест рендерит строку поверх
    состояния, а не через него. Неопознанные имена не роняют дайджест — они
    возвращаются отдельно для честного предупреждения (гейт-стоп E1 — на уровне
    агрегата). Мёртвая подветка смешанной группы видна (пустой ряд → «—»), но
    не прячется.
    """
    unknown = health_groups.unknown_groups(conn)
    from .. import taxonomy

    rows = []
    for group in taxonomy.GROUPS:
        if group == taxonomy.UNKNOWN.name:
            continue
        modes = health_groups.modes_in_group(conn, group)
        if not modes:
            # Группы нет в текущей истории вовсе — не засоряем строку.
            continue
        for mode in modes:
            agg = health_groups.aggregate(conn, group, mode, day)
            rows.append(agg)
    return {"rows": rows, "unknown": unknown}


def format_digest(state: dict) -> str:
    """Готовая карточка дня для CLI и ноутбука."""
    lines = [f"дневной дайджест: {state['today'] or '?'}"]

    rows = []
    for channel in state["channels"]:
        rows.append([
            channel.label,
            "" if channel.value is None else f"{channel.value:.2f}",
            "" if channel.z is None else f"{channel.z:+.1f}",
            "откл." if channel.bad else ("—" if channel.z is None else "в фоне"),
        ])
    if rows:
        lines.append("")
        lines.append(table(["канал", "значение", "z", "статус"], rows))
    else:
        lines.append("")
        lines.append("часов нет: каналы не считаются (нужен apple-load)")

    load = state["load"]
    lines.append("")
    if not load.get("available"):
        lines.append("нагрузка: load_daily пуст (нужен health-load)")
    else:
        lines.append(
            f"нагрузка за {load['date']}: "
            f"ACWR {_num(load['acwr'])}"
            + (f" (gap-aware {_num(load['acwr_warmup'])})"
               if load.get("acwr_warmup") is not None else "")
            + f", монотонность {_num(load['monotony'])}, "
            f"стрейн {_num(load['strain'])}"
        )
        lines.append(
            f"  тоннаж недели {_num(load['weekly_tonnage'])} кг, "
            f"дней с последней тренировки "
            f"{load['days_since_training'] if load['days_since_training'] is not None else '?'}"
        )

    gate = state.get("gate") or {}
    lines.append("")
    if gate.get("reasons") and not gate.get("ready"):
        lines.append("гейт возврата: держит — " + "; ".join(gate["reasons"]))
    elif gate:
        lines.append("гейт возврата: готов (две ночи ВСР/RHR в фоне)")
    band = _gate_band(gate)
    if band:
        lines.append(band)

    day = state.get("state") or {}
    if day:
        lines.append("состояние: " + _state_line(day))

    groups = state.get("groups") or {}
    if groups:
        lines.append(_groups_line(groups))

    score = state.get("score")
    if score is not None and score.get("score") is not None:
        tail = _score_tail(score, day)
        when = ("" if score.get("day") in (None, state.get("today"))
                else f" за {score['day']}")
        lines.append(f"скор (причинный{when}): {score['score']:.2f} — {tail}")
    if state.get("spec_warning"):
        lines.append("внимание: " + state["spec_warning"])

    rec = state["recommendation"]
    lines.append("")
    lines.append(f"рекомендация: {rec['title']}")
    lines.append(f"  {rec['text']}")
    return "\n".join(lines)


_STATE_TITLES = {
    "sick": "эпизод/красный флаг",
    "rest": "покой (гейт держит)",
    "ramp": "рампа возврата",
    "normal": "штатный план",
}


def _gate_band(gate: dict) -> str:
    """Числовая «полоса гейта» — где порог гейта против порога колонки.

    Render-only: читает **готовый** ``gate.to_dict()`` (``ready``/``reasons``/
    ``rows``/``silence_until``), новых расчётов и решений нет. Смысл строки —
    снять мнимое противоречие «канал „в фоне", а рекомендация „покой"»:
    гейт возврата держит по порогу ``|z| > GATE_Z`` (±1), а колонка каналов
    помечает «откл.» только при ``|z| > Z_CHANNEL`` (±1.5). Канал между этими
    границами честно «в фоне» и одновременно держит гейт — это разные пороги,
    а не спор. Урок E4: в строке **только числа**, без стрелок и цветов.
    """
    if not gate:
        return ""
    gate_z = health_retrain.GATE_Z
    nights = gate.get("nights") or 0
    if not nights:
        # Нет ночей для проверки (пустой aw_daily) — полосе нечего показать.
        return ""
    window_only = gate.get("silence_until") is not None and all(
        str(reason).startswith("окно тишины") for reason in gate.get("reasons", ()))
    where = ("окно тишины — рампа, не покой" if window_only
             else ("держит" if not gate.get("ready") else "готов"))
    return (f"  полоса гейта: порог ±{gate_z:g} (каналы ±{Z_CHANNEL:g}); "
            f"ночей {nights} — {where}")


def _state_line(day: dict) -> str:
    """Строка состояния по спеке §5.3/§5.6 (метка рампы видна и на паузе)."""
    label = _STATE_TITLES.get(day.get("state"), day.get("state") or "?")
    if day.get("state") in ("ramp", "rest"):
        # Пауза видна в обоих состояниях: `rest` при незавершённой рампе -
        # это «покой (рампа приостановлена)», а не безымянный покой (§5.3).
        # Метка требует начатой рампы (`ramp_day > 0`): покой без эпизода и
        # без рампы - не «пауза», а просто покой.
        k = day.get("ramp_day", 0) or 0
        if day.get("ramp_paused") and (day.get("state") == "ramp" or k):
            return f"{label} (рампа приостановлена)"
        if day.get("state") == "ramp":
            n = day.get("ramp_days", 0)
            # k = выполненные дни рампы; пока не выполнено ни дня - числа нет
            return label if not k else f"{label}: день {k} из {n}"
        return label
    if day.get("state") == "sick" and day.get("reason"):
        return f"{label} — {day['reason']}"
    return label


def _groups_line(groups: dict) -> str:
    """Описательная E-строка (E4 v1): числа и ``n_days``, без порогов/стрелок.

    Никаких интерпретационных меток: порог в рендере — контрабанда решения,
    прямо против E4 «решение не меняет». Метрика даётся по ветке (тоннаж /
    подходы / время), единица — как в базе.
    """
    rows = groups.get("rows") or []
    if not rows:
        return "по группам: нет данных"
    units = {"tonnage": "кг", "sets": "подх.", "seconds": "с"}
    parts = []
    for row in rows:
        metric = row["metric"]
        value = row["value"]
        mode = row["mode"]
        if value is None:
            # Мёртвая/тонкая подветка: «—», одно правило для обеих (§2.5).
            shown = "—"
        else:
            shown = f"{value:.0f} {units.get(metric, metric)}"
        parts.append(
            f"{row['group'].lower()}/{mode.value} {shown} (n={row['n_days']})"
        )
    line = "по группам: " + "; ".join(parts)
    unknown = groups.get("unknown") or []
    if unknown:
        line += f"; не опознано: {', '.join(unknown)}"
    return line


def _score_tail(score: dict, day: dict) -> str:
    """Хвост строки скора: тихий мониторинг, а не решение (после causal replay).

    Скор не меняет состояние ни в одном случае - только поясняет (§5.6).
    """
    if score.get("threshold") is None:
        return "порог ещё не накоплен"
    if score.get("alert"):
        return f"выше порога {score['threshold']:.2f} (алерт)"
    state = day.get("state")
    if state == "sick":
        return "в эпизоде, скор не решает"
    if state in ("ramp", "rest"):
        decision = "рампа" if state == "ramp" else "покой"
        return f"мониторинг (порог {score['threshold']:.2f}), решение — {decision}"
    return f"мониторинг (порог {score['threshold']:.2f})"


def _num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def dumps(value) -> str:
    """JSON-представление для ``--json``."""
    return json.dumps(_jsonable(value), ensure_ascii=False, indent=2, default=str)
