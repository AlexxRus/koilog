"""Перезапуск ячеек ноутбука без ядра: exec в общем пространстве имён.

Перехватывается stdout/stderr каждой ячейки и matplotlib-фигуры (Agg ->
base64 PNG), потому что nbclient/jupyter в окружении нет. Формат JSON
сохраняется байт-в-байт (indent=1, ensure_ascii=False, хвостовой \\n).

Запуск:  python tools/nb_run.py <имя.ipynb> [ячейки: 2,4,6] [прелюдия: 12]
Без списка ячеек выполняются все code-ячейки по порядку. Ячейки-прелюдии
выполняются в том же пространстве имён (например, ставят rcParams), но их
сохранённый вывод не переписывается.

Корень репозитория определяется от расположения этого файла (или переменной
окружения ``KOILOG_ROOT``); рабочий каталог переключается на него, поэтому имя
ноутбука задаётся относительно корня.
"""
import base64
import contextlib
import io
import json
import os
import sys
import time
import warnings

ROOT = os.environ.get(
    "KOILOG_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(name: str, only: set[int] | None,
        setup: set[int] | None = None) -> None:
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.chdir(ROOT)
    path = os.path.join(ROOT, name)
    nb = json.load(io.open(path, encoding="utf-8"))
    setup = setup or set()

    ns: dict = {"__name__": "__main__"}
    try:                      # чтобы прогонять только ячейки, которым нужен plt
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ns["plt"] = plt
    except ImportError:
        pass
    count = 0
    log = []
    for i, cell in enumerate(nb["cells"]):
        if cell["cell_type"] != "code":
            continue
        if i in setup:
            # Ячейка-прелюдия (например, rcParams/импорты): выполняем, но её
            # вывод не трогаем - он в ноутбуке остаётся прежним.
            source = "\n".join(
                line for line in "".join(cell["source"]).splitlines()
                if not line.startswith("%"))
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                exec(compile(source, f"<{name} cell {i}>", "exec"), ns)
            try:
                import matplotlib.pyplot as plt
                plt.close("all")
            except ImportError:
                pass
            continue
        if only is not None and i not in only:
            continue
        count += 1
        source = "".join(cell["source"])
        source = "\n".join(line for line in source.splitlines()
                           if not line.startswith("%"))
        t0 = time.time()
        out, err = io.StringIO(), io.StringIO()
        try:
            with warnings.catch_warnings():
                # Jupyter с inline-бэкендом этого не печатает: глушим шум
                # «FigureCanvasAgg is non-interactive», который даёт Agg.
                warnings.filterwarnings(
                    "ignore", message=".*is non-interactive.*")
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exec(compile(source, f"<{name} cell {i}>", "exec"), ns)
        except BaseException as exc:
            print(f" cell {i}: ПРОВАЛ {type(exc).__name__}: {exc}")
            raise
        outputs = []
        if out.getvalue():
            outputs.append({"output_type": "stream", "name": "stdout",
                            "text": _lines(out.getvalue())})
        if err.getvalue():
            outputs.append({"output_type": "stream", "name": "stderr",
                            "text": _lines(err.getvalue())})
        outputs.extend(_figures())
        cell["outputs"] = outputs
        # Номер = порядковый номер code-ячейки в ноутбуке (как при прогоне
        # всего блокнота сверху вниз), а не номер в текущем прогоне: иначе
        # перезапуск отдельных ячеек ломает нумерацию.
        cell["execution_count"] = sum(
            1 for c in nb["cells"][:i + 1] if c["cell_type"] == "code")
        log.append(f" cell {i}: ok {time.time() - t0:.1f}s, "
                   f"stdout {len(out.getvalue())} симв.")

    io.open(path, "w", encoding="utf-8", newline="").write(
        json.dumps(nb, indent=1, ensure_ascii=False) + "\n")
    print(f"{name}: выполнено ячеек {count}")
    for line in log:
        print(line)


def _lines(text: str) -> list[str]:
    """Jupyter хранит text как список строк, а не одной строкой."""
    return text.splitlines(keepends=True) or [""]


def _figures() -> list[dict]:
    """Незакрытые matplotlib-фигуры - в display_data, как inline."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    out = []
    for num in plt.get_fignums():
        fig = plt.figure(num)
        buf = io.BytesIO()
        # dpi не задаём: как inline, рисуем в dpi фигуры (rcParams figure.dpi).
        fig.savefig(buf, format="png", bbox_inches="tight")
        out.append({
            "output_type": "display_data",
            "data": {
                "image/png": base64.b64encode(buf.getvalue()).decode("ascii"),
                "text/plain": _lines(repr(fig)),
            },
            "metadata": {},
        })
        plt.close(fig)
    return out


if __name__ == "__main__":
    nb_name = sys.argv[1]
    cells = ({int(x) for x in sys.argv[2].split(",")} if len(sys.argv) > 2
             else None)
    setup = ({int(x) for x in sys.argv[3].split(",")}
             if len(sys.argv) > 3 else None)
    run(nb_name, cells, setup)
