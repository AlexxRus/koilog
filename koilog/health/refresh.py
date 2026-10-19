"""Единый шаг обновления: свежие данные из выгрузки в базу и в ноутбуки.

Звенья конвейера связаны, но до сих пор запускались вручную и вразнобой:
``apple-load`` пишет ``aw_*``, ``health-load`` пересобирает ``load_daily`` и
``health_days``, ``illness`` - эпизоды, а ``ml_*`` догоняет только ML-отчёт.
Каждое звено читает то, что написало предыдущее, поэтому пропуск любого из
них даёт **тихое** отставание: ноутбук печатает старую дату без ошибок.

Функции здесь живут отдельно от :mod:`koilog.cli`, чтобы ноутбук мог
позвать шаг без argparse и без печати в stdout. CLI зовёт их же.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import apple, illness
from . import load as health_load
from . import text as health_text

__all__ = ["REFRESH_KEY", "Step", "run", "format_freshness"]

#: Ключ слепка источника в ``aw_meta``: mtime + размер разобранного файла.
#: Хранится там же, где ``export_date`` из самой выгрузки.
REFRESH_KEY = "refresh_source"

#: Таблицы, по которым видна свежесть ряда, и колонка даты в каждой.
_FRESHNESS: tuple[tuple[str, str], ...] = (
    ("aw_daily", "date"),
    ("load_daily", "date"),
    ("health_days", "date"),
    ("ml_features", "date"),
    ("ml_score", "day"),
    ("illness_episodes", "start"),
)


@dataclass(slots=True)
class Step:
    """Один прогон шага: что сделано и насколько свежи таблицы."""

    deep: bool = False
    parsed: bool = False                     # разбирали ли выгрузку целиком
    source: Path | None = None               # откуда читали
    export_date: str | None = None           # export_date из выгрузки
    stages: list[str] = field(default_factory=list)
    freshness: list[tuple[str, str | None]] = field(default_factory=list)


def _source_stamp(path: Path | None) -> str | None:
    """Слепок источника: mtime и размер, без пути (путь личный)."""
    if path is None or not path.exists():
        return None
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    if not _has_table(conn, "aw_meta"):
        return None
    row = conn.execute(
        "SELECT value FROM aw_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _need_parse(conn: sqlite3.Connection, source: Path) -> bool:
    """Разбирать ли выгрузку: только если файл изменился с прошлого шага.

    Разбор - один проход по XML на гигабайт и ~45 секунд, поэтому он и
    скипается. Слепок пишется **после** успешной записи ``aw_*``: если
    разбор упал, следующий шаг попробует снова.
    """
    return _source_stamp(source) != _meta_get(conn, REFRESH_KEY)


def _freshness(conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    """Максимальная дата каждой таблицы - чтобы отставание было видно."""
    out = []
    for table, column in _FRESHNESS:
        if not _has_table(conn, table):
            out.append((table, None))
            continue
        row = conn.execute(
            f"SELECT MAX({column}) FROM {table}").fetchone()
        out.append((table, row[0] if row else None))
    return out


def run(
    conn: sqlite3.Connection,
    deep: bool = False,
    source: str | Path | None = None,
) -> Step:
    """Прогнать конвейер целиком: часы -> нагрузка -> эпизоды -> (-> ML).

    ``deep`` - пересобрать ML-блок: ``ml_features``/``ml_labels`` (чистая
    стандартная библиотека) и, если есть sklearn, **ledger дневного скора**
    (:mod:`koilog.health.score`). Он требует numpy/scikit-learn (extra
    ``ml``), которых нет у ноутбука журнала, поэтому в обычном шаге ML-блок
    не трогается: ``koilog.ipynb`` не должен тянуть numpy ради дайджеста.

    Ledger скора живёт именно здесь, потому что его ценность - в записи
    **в тот день, когда исход ещё не известен** (append-only, без
    перезаписи - см. :mod:`koilog.health.score`). Первый прогон на чистой
    базе засевает историю причинным воспроизведением, дальше добавляется
    только последний день.
    """
    resolved = None
    try:
        resolved = apple.resolve_source(source)
    except FileNotFoundError:
        resolved = None

    step = Step(deep=deep, source=resolved.path if resolved else None)

    if resolved is None:
        step.stages.append("выгрузка (нет файла — работаю от базы)")
    elif _need_parse(conn, resolved.path):
        # парсим и пишем в одно соединение шага: иначе тесты на временной
        # базе молча переписывали бы боевую (apple.load открывает свою)
        data = apple.parse_apple(resolved)
        apple.write_apple(conn, data)
        conn.commit()
        stamp = _source_stamp(resolved.path)  # слепок - только после успеха
        conn.execute(
            "INSERT OR REPLACE INTO aw_meta VALUES (?, ?)",
            (REFRESH_KEY, stamp),
        )
        conn.commit()
        step.parsed = True

    if resolved is not None:
        step.stages.append("выгрузка" + (" (разобрана)" if step.parsed
                                         else " (файл не менялся - скип)"))

    step.export_date = _meta_get(conn, "export_date")

    health_load.load_daily(conn)
    step.stages.append("нагрузка")
    health_load.health_days(conn)
    step.stages.append("календарь")
    illness.build(conn)
    step.stages.append("эпизоды")

    if deep:
        if not _has_table(conn, "load_daily"):
            raise RuntimeError("нет load_daily - сначала: health-load")
        from . import ml as health_ml
        health_ml.build_features(conn)
        health_ml.build_labels(conn)
        step.stages.append("ML-признаки")
        if health_ml.HAS_ML:
            # ledger дня пишется здесь: пока исход не известен, запись
            # проспективная, а перезапись задним числом её бы стёрла
            from . import score as health_score
            summary = health_score.sync(conn)
            if summary.get("written"):
                step.stages.append(f"скор (записан {summary.get('latest') or summary.get('day')})")
            elif summary.get("seeded"):
                step.stages.append("скор (ledger посеян, новых дней нет)")
            else:
                step.stages.append(
                    f"скор (день не оценён: "
                    f"{health_score.reason_text(summary.get('reason'))})")

    step.freshness = _freshness(conn)
    return step


def format_freshness(step: Step) -> str:
    """Итог шага: звенья строкой и свежесть таблицей «таблица | дата»."""
    lines = [f"обновление: {', '.join(step.stages)}"]
    if step.export_date:
        lines.append(f"выгрузка от {step.export_date}")
    body = health_text.table(("таблица", "дата"), list(step.freshness))
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)
