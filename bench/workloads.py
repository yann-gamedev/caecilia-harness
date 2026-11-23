"""Benchmark workloads: small but real Git repos with tests, plus the plan to run on them.

- W1: two independent packages (`api/`, `web/`), 4 tasks, separate write sets, no dependencies.
- W2: a chain of 3 dependent tasks (rename a function, update its callers, add tests).
- W3: 6 small independent edits in different modules, each with its test.

Every task owns a folder (`write_set = ["<folder>/**"]`), so tasks that may run in parallel
never overlap, and the fake worker's note always lands inside the task's write set. The check
is the repo's own test suite (stdlib `unittest`, so no dependency is needed in the repo).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workload:
    name: str
    description: str
    files: dict[str, str]
    plan: str

    @property
    def check(self) -> list[str]:
        """The gate's check command: run every `test_*.py` in the repo."""
        return [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]


def _task(task_id: str, title: str, write_set: list[str], instructions: str, **extra: str) -> str:
    lines = [
        "[[task]]",
        f'id = "{task_id}"',
        f'title = "{title}"',
        f'instructions = """\n{instructions.strip()}\n"""',
        "write_set = [" + ", ".join(f'"{p}"' for p in write_set) + "]",
    ]
    lines += [f"{key} = {value}" for key, value in extra.items()]
    return "\n".join(lines)


def _plan(goal: str, tasks: list[str]) -> str:
    return "\n\n".join([f'version = 1\ngoal = "{goal}"', *tasks]) + "\n"


_W1_FILES = {
    "README.md": "# W1\nTwo packages: `api/` (backend helpers) and `web/` (page rendering).\n",
    "api/__init__.py": "",
    "api/health/__init__.py": "",
    "api/health/status.py": 'def status() -> dict[str, str]:\n    return {"status": "ok"}\n',
    "api/health/test_status.py": (
        "import unittest\n\nfrom api.health.status import status\n\n\n"
        "class StatusTest(unittest.TestCase):\n"
        "    def test_ok(self) -> None:\n"
        '        self.assertEqual(status(), {"status": "ok"})\n'
    ),
    "api/version/__init__.py": "",
    "api/version/info.py": 'VERSION = "1.0.0"\n',
    "web/__init__.py": "",
    "web/status/__init__.py": "",
    "web/status/page.py": 'def render(status: str) -> str:\n    return f"<p>{status}</p>"\n',
    "web/status/test_page.py": (
        "import unittest\n\nfrom web.status.page import render\n\n\n"
        "class PageTest(unittest.TestCase):\n"
        "    def test_render(self) -> None:\n"
        '        self.assertEqual(render("ok"), "<p>ok</p>")\n'
    ),
    "web/footer/__init__.py": "",
    "web/footer/footer.py": 'def footer() -> str:\n    return "<footer></footer>"\n',
}

W1 = Workload(
    name="W1",
    description="dua paket independen (api/, web/), 4 task tanpa dependensi",
    files=_W1_FILES,
    plan=_plan(
        "Lengkapi api dan web",
        [
            _task(
                "api-health",
                "Detail health",
                ["api/health/**"],
                """
In api/health/status.py add a function `detailed_status() -> dict[str, str]` that returns
{"status": "ok", "service": "api"}. Add a unittest for it in api/health/test_status.py.
Keep `status()` unchanged.
""",
            ),
            _task(
                "api-version",
                "Versi API",
                ["api/version/**"],
                """
In api/version/info.py add a function `version_tuple() -> tuple[int, int, int]` that parses
VERSION into integers. Add api/version/test_info.py with a unittest for it.
""",
            ),
            _task(
                "web-status",
                "Status bertanda",
                ["web/status/**"],
                """
In web/status/page.py add a function `render_badge(status: str) -> str` that returns
'<span class="badge">' + status + '</span>'. Add a unittest for it in web/status/test_page.py.
Keep `render()` unchanged.
""",
            ),
            _task(
                "web-footer",
                "Footer dengan teks",
                ["web/footer/**"],
                """
Change web/footer/footer.py so `footer(text: str = "")` returns "<footer>" + text + "</footer>".
Add web/footer/test_footer.py with unittests for the empty and the non-empty case.
""",
            ),
        ],
    ),
)

_W2_FILES = {
    "README.md": "# W2\n`lib/` computes order totals, `app/` uses them, `tests/` checks them.\n",
    "lib/__init__.py": "",
    "lib/totals.py": (
        "def calc_total(prices: list[float]) -> float:\n    return round(sum(prices), 2)\n"
    ),
    "app/__init__.py": "",
    "app/orders.py": (
        "from lib.totals import calc_total\n\n\n"
        "def order_total(items: list[tuple[str, float]]) -> float:\n"
        "    return calc_total([price for _, price in items])\n"
    ),
    "tests/__init__.py": "",
    "tests/test_orders.py": (
        "import unittest\n\nfrom app.orders import order_total\n\n\n"
        "class OrdersTest(unittest.TestCase):\n"
        "    def test_total(self) -> None:\n"
        '        self.assertEqual(order_total([("a", 1.25), ("b", 2.5)]), 3.75)\n'
    ),
}

W2 = Workload(
    name="W2",
    description="rantai 3 task yang saling bergantung (ganti nama, perbarui pemanggil, tes)",
    files=_W2_FILES,
    plan=_plan(
        "Ganti nama calc_total menjadi compute_total",
        [
            _task(
                "rename",
                "Ganti nama fungsi",
                ["lib/**"],
                """
In lib/totals.py rename `calc_total` to `compute_total`. Keep `calc_total` as a thin alias
that calls `compute_total`, so existing callers keep working.
""",
            ),
            _task(
                "callers",
                "Perbarui pemanggil",
                ["app/**"],
                """
Update app/orders.py to import and call `compute_total` instead of `calc_total`.
""",
                blocked_by='["rename"]',
            ),
            _task(
                "tests",
                "Tes compute_total",
                ["tests/**"],
                """
Add tests/test_totals.py with unittests for `lib.totals.compute_total`: an empty list gives 0,
and rounding to two decimals works. Keep tests/test_orders.py passing.
""",
                blocked_by='["callers"]',
            ),
        ],
    ),
)


def _w3_module(index: int) -> dict[str, str]:
    folder = f"e{index}"
    return {
        f"{folder}/__init__.py": "",
        f"{folder}/text.py": f'def label() -> str:\n    return "item {index}"\n',
        f"{folder}/test_text.py": (
            f"import unittest\n\nfrom {folder}.text import label\n\n\n"
            "class LabelTest(unittest.TestCase):\n"
            "    def test_label(self) -> None:\n"
            f'        self.assertEqual(label(), "item {index}")\n'
        ),
    }


_W3_FILES = {"README.md": "# W3\nSix independent modules `e1/` .. `e6/`, each with a test.\n"}
for _index in range(1, 7):
    _W3_FILES |= _w3_module(_index)

W3 = Workload(
    name="W3",
    description="6 edit kecil independen di file berbeda, masing-masing dengan tes",
    files=_W3_FILES,
    plan=_plan(
        "Enam perubahan kecil",
        [
            _task(
                f"edit{i}",
                f"Label besar {i}",
                [f"e{i}/**"],
                f"""
In e{i}/text.py add a function `shout() -> str` that returns `label().upper()`.
Add a unittest for it in e{i}/test_text.py. Keep `label()` unchanged.
""",
            )
            for i in range(1, 7)
        ],
    ),
)

WORKLOADS: dict[str, Workload] = {w.name: w for w in (W1, W2, W3)}


def _git(repo: Path, *args: str) -> None:
    exe = shutil.which("git")
    if exe is None:
        raise RuntimeError("git is not installed")
    subprocess.run([exe, *args], cwd=repo, check=True, capture_output=True)


def create_repo(workload: Workload, folder: Path) -> Path:
    """Write the workload's files into a fresh Git repo (branch `main`, one commit)."""
    folder.mkdir(parents=True, exist_ok=False)
    for rel, content in workload.files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "config", "user.name", "caecilia-bench")
    _git(folder, "config", "user.email", "bench@localhost")
    _git(folder, "config", "core.autocrlf", "false")
    _git(folder, "config", "commit.gpgsign", "false")
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", f"benchmark workload {workload.name}")
    return folder
