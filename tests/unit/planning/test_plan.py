"""`plan.toml` model, reading, writing and conversion (backlog M2-02)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from caecilia_harness.core.models import Budget
from caecilia_harness.planning.plan import (
    Plan,
    PlanBudget,
    PlanError,
    PlanTask,
    dump_plan,
    load_plan,
    parse_plan,
    plan_id,
    plan_to_specs,
    single_task_plan,
)

ROOT = Path(__file__).resolve().parents[3]
PLANS = ROOT / "tests" / "fixtures" / "plans"


def _architecture_example() -> str:
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    # The doc also shows a `.caecilia.toml`; the plan example is the block with tasks.
    [block] = [
        b for b in re.findall(r"```toml\n(.*?)```", text, flags=re.DOTALL) if "[[task]]" in b
    ]
    return block


def test_example_in_architecture_doc_is_valid() -> None:
    plan = parse_plan(_architecture_example())
    assert plan.max_parallel == 2 and plan.target == "main"
    assert plan.budget == PlanBudget(max_input_tokens=2_000_000, max_output_tokens=200_000)
    api, web = plan.tasks
    assert (api.id, api.write_set, api.worker, api.budget) == (
        "api",
        ["api/**", "tests/api/**"],
        "native",
        PlanBudget(max_steps=40),
    )
    assert api.instructions.startswith("Tambahkan endpoint GET /health")
    assert (web.id, web.blocked_by, web.worker, web.idempotent) == ("web", ["api"], None, True)


def test_fixture_plans_load() -> None:
    assert [t.id for t in load_plan(PLANS / "chain.toml").tasks] == ["api", "web"]
    assert [t.id for t in load_plan(PLANS / "independent.toml").tasks] == ["zeta", "alpha"]


def test_broken_toml_reports_the_line() -> None:
    text = 'version = 1\ngoal = "x"\n\n[[task]\nid = "a"\n'
    with pytest.raises(PlanError) as info:
        parse_plan(text, "plan.toml")
    [problem] = info.value.problems
    assert "TOML tidak valid" in problem
    assert "line 4" in problem
    assert str(info.value).startswith("plan.toml: ")


def test_missing_file() -> None:
    with pytest.raises(PlanError, match="tidak bisa membaca"):
        load_plan(PLANS / "does-not-exist.toml")


HEAD = 'version = 1\ngoal = "g"\n'
TASK = '[[task]]\nid = "a"\ntitle = "t"\ninstructions = "i"\n'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (HEAD + 'colour = "red"\n' + TASK, "colour"),
        (HEAD + TASK + "prio = 1\n", "prio"),
        (HEAD + "[budget]\nmax_tokens = 5\n" + TASK, "max_tokens"),
    ],
    ids=["top-level", "task", "budget"],
)
def test_unknown_fields_are_rejected(text: str, expected: str) -> None:
    with pytest.raises(PlanError) as info:
        parse_plan(text)
    assert any(expected in problem for problem in info.value.problems)


def test_structure_errors_are_all_reported() -> None:
    text = 'version = 2\n[[task]]\nid = "a"\ninstructions = 3\n'
    with pytest.raises(PlanError) as info:
        parse_plan(text)
    joined = "\n".join(info.value.problems)
    for field in ("version", "goal", "task.0.title", "task.0.instructions"):
        assert field in joined, joined


def test_plan_needs_at_least_one_task() -> None:
    with pytest.raises(PlanError, match="task"):
        parse_plan('version = 1\ngoal = "g"\n')


def test_graph_problems_are_reported_together() -> None:
    with pytest.raises(PlanError) as info:
        load_plan(PLANS / "broken_graph.toml")
    problems = info.value.problems
    joined = "\n".join(problems)
    assert "missing_blocker" in joined and "ghost" in joined
    assert "bad_pattern" in joined and "../outside/**" in joined
    assert "duplicate_id" in joined
    assert "cycle" in joined and "a -> b -> a" in joined
    assert len(problems) == 4


# --- dump / load ----------------------------------------------------------------------------------

TRICKY = 'Baris satu dengan "kutip" dan \\backslash\\.\n\tTab, """tiga kutip""", dan C:\\path\\x\n'


def test_dump_then_load_is_identity() -> None:
    plan = Plan(
        version=1,
        goal='Goal dengan "kutip"',
        target="release/1.0",
        max_parallel=3,
        budget=PlanBudget(max_input_tokens=1000, max_cost_usd=2.5),
        tasks=[
            PlanTask(
                id="api",
                title="Judul \\ aneh",
                instructions=TRICKY,
                acceptance=['GET /health -> {"status": "ok"}', "kedua"],
                write_set=["api/**", "tests/api/*.py"],
                worker="native",
                idempotent=False,
                budget=PlanBudget(max_steps=40),
            ),
            PlanTask(id="web", title="Web", instructions="satu baris", blocked_by=["api"]),
        ],
    )
    text = dump_plan(plan)
    assert parse_plan(text) == plan
    assert '"""' in text  # multi-line instructions use a multi-line string


def test_architecture_example_round_trips() -> None:
    plan = parse_plan(_architecture_example())
    assert parse_plan(dump_plan(plan)) == plan


_texts = st.text(max_size=60)


@settings(max_examples=200, deadline=None)
@given(goal=_texts, title=_texts, instructions=_texts, acceptance=st.lists(_texts, max_size=3))
def test_any_strings_round_trip(
    goal: str, title: str, instructions: str, acceptance: list[str]
) -> None:
    plan = Plan(
        version=1,
        goal=goal,
        tasks=[PlanTask(id="t", title=title, instructions=instructions, acceptance=acceptance)],
    )
    assert parse_plan(dump_plan(plan)) == plan


# --- conversion -----------------------------------------------------------------------------------


def test_plan_to_specs() -> None:
    plan = parse_plan(_architecture_example())
    api, web = plan_to_specs(plan, "r1", default_worker="fake")
    assert (api.id, api.run_id, api.worker, api.blocked_by) == ("r1-api", "r1", "native", [])
    assert api.budget == Budget(max_steps=40)
    assert api.acceptance == plan.tasks[0].acceptance
    assert (web.id, web.worker, web.blocked_by, web.budget) == (
        "r1-web",
        "fake",
        ["r1-api"],
        Budget(),
    )
    assert web.write_set == ["web/**"]
    assert plan_id(web.id, "r1") == "web"


def test_empty_write_set_becomes_whole_repo() -> None:
    plan = Plan(version=1, goal="g", tasks=[PlanTask(id="a", title="a", instructions="i")])
    [spec] = plan_to_specs(plan, "r1", "fake")
    assert spec.write_set == ["**"]


def test_single_task_plan() -> None:
    plan = single_task_plan("tambahkan catatan")
    [task] = plan.tasks
    assert (task.id, task.title, task.instructions, task.write_set) == (
        "main",
        "tambahkan catatan",
        "tambahkan catatan",
        ["**"],
    )
    long_goal = "x" * 200
    assert len(single_task_plan(long_goal).tasks[0].title) == 80
    assert single_task_plan("g", idempotent=False).tasks[0].idempotent is False
    assert parse_plan(dump_plan(plan)) == plan
