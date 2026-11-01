"""Контракт сериализации ноутбуков (NB-98).

Проверяется не поведение ``koilog.*``, а форма ``.ipynb``, которую пишет
драйвер ``tools/nb_run.py`` (D0+D2; фигуры — D1 под skip). Живые ноутбуки
читаются только для байт-пина round-trip (`docs/nb-98-notebook-serialization.md`
§4–§5): их исполнение здесь не запускается.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tools" / "nb_run.py"

try:
    import matplotlib  # noqa: F401
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


def _write(nb: dict, path: Path) -> None:
    path.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="")


def _run(directory: Path, name: str, cells: str | None = None,
         setup: str | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, str(DRIVER), name]
    if cells:
        args.append(cells)
    if setup:
        args.append(setup)
    env = dict(os.environ, KOILOG_ROOT=str(directory), MPLBACKEND="Agg")
    return subprocess.run(
        args, cwd=str(directory), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")


def _probe_nb(*, ids: bool = True, minor: int = 5) -> dict:
    cells = [
        {"cell_type": "markdown", "id": "md-1" if ids else None,
         "metadata": {}, "source": ["# Т\n"]},
        {"cell_type": "code", "id": "code-1" if ids else None,
         "execution_count": None, "metadata": {}, "outputs": [],
         "source": ["print('первая')\n", "print('вторая')\n"]},
        {"cell_type": "code", "id": "code-2" if ids else None,
         "execution_count": None, "metadata": {}, "outputs": [],
         "source": ["x = 1\n", "x + 1\n"]},
    ]
    for c in cells:
        if "id" in c and c["id"] is None:
            del c["id"]
    return {
        "cells": cells,
        "metadata": {"kernelspec": {"display_name": "probe", "language": "python",
                                    "name": "python3"},
                     "language_info": {"name": "python", "version": "3.13.3"}},
        "nbformat": 4,
        "nbformat_minor": minor,
    }


class NotebookSerializationTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _probe(self, *args, **kwargs):
        path = self.dir / "probe.ipynb"
        _write(_probe_nb(*args, **kwargs), path)
        return path

    def test_t1_canonical_shape(self):
        path = self._probe()
        res = _run(self.dir, "probe.ipynb")
        self.assertEqual(res.returncode, 0, res.stderr)
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        nb = json.loads(text)
        self.assertEqual(
            json.dumps(nb, indent=1, ensure_ascii=False) + "\n", text)
        self.assertNotIn("\r", text)
        self.assertEqual(nb["nbformat"], 4)
        self.assertEqual(nb["nbformat_minor"], 4)
        for cell in nb["cells"]:
            self.assertNotIn("id", cell)
            if cell["cell_type"] == "code":
                for out in cell["outputs"] or []:
                    if "text" in out:
                        self.assertIsInstance(out["text"], list)
        code = [c for c in nb["cells"] if c["cell_type"] == "code"]
        self.assertEqual(
            [c["execution_count"] for c in code], [1, 2])

    def test_t2_deterministic_rerun(self):
        path = self._probe()
        first = _run(self.dir, "probe.ipynb")
        self.assertEqual(first.returncode, 0, first.stderr)
        after_first = path.read_bytes()
        second = _run(self.dir, "probe.ipynb")
        self.assertEqual(second.returncode, 0, second.stderr)
        after_second = path.read_bytes()
        self.assertEqual(after_second, after_first)

    def test_t3_failure_leaves_file_untouched(self):
        nb = _probe_nb()
        nb["cells"].append({"cell_type": "code", "id": "code-3",
                            "execution_count": None, "metadata": {},
                            "outputs": [], "source": ["1/0\n"]})
        path = self.dir / "probe.ipynb"
        _write(nb, path)
        before = path.read_bytes()
        res = _run(self.dir, "probe.ipynb", cells="3")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(path.read_bytes(), before)

    def test_t4_magic_and_empty_streams(self):
        nb = _probe_nb()
        nb["cells"] = [
            {"cell_type": "code", "execution_count": None, "metadata": {},
             "outputs": [], "source": ["%matplotlib inline\n", "y = 2\n"]},
            {"cell_type": "code", "execution_count": None, "metadata": {},
             "outputs": [], "source": ["print(end='')\n"]},
        ]
        for c in nb["cells"]:
            c.pop("id", None)
        path = self.dir / "probe.ipynb"
        _write(nb, path)
        res = _run(self.dir, "probe.ipynb")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["cells"][0]["source"], ["%matplotlib inline\n", "y = 2\n"])
        self.assertEqual(data["cells"][1]["outputs"], [])

    def test_t5_metadata_untouched(self):
        path = self._probe()
        before = json.loads(path.read_text(encoding="utf-8"))["metadata"]
        _run(self.dir, "probe.ipynb")
        after = json.loads(path.read_text(encoding="utf-8"))["metadata"]
        self.assertEqual(after, before)

    def test_t6_minor_44_and_no_ids(self):
        path = self._probe(ids=True, minor=5)
        res = _run(self.dir, "probe.ipynb")
        self.assertEqual(res.returncode, 0, res.stderr)
        nb = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(nb["nbformat_minor"], 4)
        for cell in nb["cells"]:
            self.assertNotIn("id", cell)

    @unittest.skipUnless(HAS_MPL, "нет matplotlib — проверка фигуры пропущена")
    def test_t7_figure_becomes_display_data(self):
        nb = _probe_nb()
        nb["cells"] = [
            {"cell_type": "code", "execution_count": None, "metadata": {},
             "outputs": [],
             "source": ["import matplotlib.pyplot as plt\n",
                        "fig, ax = plt.subplots()\n", "ax.plot([1, 2])\n"]},
        ]
        for c in nb["cells"]:
            c.pop("id", None)
        path = self.dir / "probe.ipynb"
        _write(nb, path)
        res = _run(self.dir, "probe.ipynb")
        self.assertEqual(res.returncode, 0, res.stderr)
        nb_out = json.loads(path.read_text(encoding="utf-8"))["cells"][0]["outputs"]
        self.assertTrue(any(o["output_type"] == "display_data" for o in nb_out))
        png = next(o["data"]["image/png"] for o in nb_out
                   if o["output_type"] == "display_data")
        self.assertIsInstance(png, str)
        self.assertTrue(png)

    def test_t8_live_notebooks_roundtrip(self):
        for name in ("koilog.ipynb", "koilog-health.ipynb"):
            with self.subTest(name=name):
                path = ROOT / name
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                self.assertNotIn("\r", text)
                nb = json.loads(text)
                self.assertEqual(
                    json.dumps(nb, indent=1, ensure_ascii=False) + "\n", text)


if __name__ == "__main__":
    unittest.main()