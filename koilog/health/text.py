"""Простое текстовое оформление для CLI и ноутбука.

Вывод ``health``-команд должен быть одинаковым и в терминале, и в ячейке,
поэтому таблицы рисуются здесь, а не в ``cli.py``. Логики тут нет - только
выравнивание колонок по фактической ширине.
"""

from __future__ import annotations

from typing import Sequence

__all__ = ["table"]


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Собрать таблицу с заголовком и линейкой; пусто - пустая строка."""
    if not rows:
        return ""
    cells = [[("" if value is None else str(value)) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    header = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines = [header, "-" * len(header)]
    for row in cells:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)))
    return "\n".join(lines)
