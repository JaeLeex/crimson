from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crimson.minds.build import (
    SIZES,
    BuildConfig,
    BuildPolicy,
    Playtest,
    PlaytestRule,
    Proposal,
    ScriptedBuilder,
    Wave,
    audit_build,
    judge,
    policy_violations,
    run_build,
    waves_recipe,
    write_build,
)
from crimson.quests.dsl import build_custom_quest


def _quest(waves: list[Wave]):
    return build_custom_quest(waves_recipe(waves, title="test", author="tests"))


@pytest.mark.parametrize("shape", ["ring", "side", "pincer", "corners"])
def test_every_shape_compiles_to_its_size(shape: str) -> None:
    quest = _quest([Wave(0, "alien.green", shape, "medium")])
    assert sum(entry.count for entry in quest.entries) == SIZES["medium"]


def test_policy_allows_the_menu_it_lists() -> None:
    quest = _quest([Wave(seconds, "alien.small_gray", "ring", "medium") for seconds in (0, 8, 16, 24)])
    assert policy_violations(quest, BuildPolicy()) == []


def test_policy_names_forbidden_templates_size_and_time() -> None:
    quest = _quest([
        Wave(0, "nest.alien", "corners", "small"),
        Wave(8, "alien.boss", "side", "large"),
        Wave(16, "alien.green", "ring", "large"),
        Wave(40, "alien.green", "ring", "large"),
    ])
    violations = policy_violations(quest, BuildPolicy())
    assert "nest.alien not allowed (x8)" in violations
    assert "alien.boss not allowed (x22)" in violations
    assert "74 creatures > 64" in violations
    assert "last wave at 40s > 30s" in violations


def _test(outcome: str, seconds: float, health: float) -> Playtest:
    return Playtest(seed=1, outcome=outcome, seconds=seconds, health=health, kills=0, replay_sha256="", replay_file="")


def test_judge_needs_a_clear_survival_and_a_fight() -> None:
    rule = PlaytestRule()
    assert judge([_test("death", 40, 0)] * 4, rule)[0] is False
    assert judge([_test("quest_completed", 30, 50), _test("death", 5, 0)], rule)[0] is False
    assert judge([_test("quest_completed", 30, 100)] * 4, rule)[0] is False
    assert judge([_test("quest_completed", 30, 60), _test("death", 25, 0)], rule)[0] is True


class _ForbiddenBuilder:
    """Always proposes a boss wave, at a fixed token count."""

    name = "forbidden"
    model = "test-model"

    def propose(self, attempt: int, brief: dict[str, Any]) -> Proposal:
        recipe = waves_recipe([Wave(0, "alien.boss", "side", "small")], title="boss", author="tests")
        return Proposal(recipe=recipe, prompt_tokens=2500)


def test_governed_builder_is_refused_before_play_and_stops_at_budget(tmp_path: Path) -> None:
    run = run_build(_ForbiddenBuilder(), BuildConfig(attempts=10, budget=9, rule=PlaytestRule(seeds=(1,))), tmp_path)
    verdicts = [attempt.verdict for attempt in run.attempts]
    assert verdicts == ["refused_policy"] * 3 + ["refused_budget"]
    assert run.account.spent == 9
    assert not any(attempt.playtests for attempt in run.attempts)
    assert not list((tmp_path / "playtests").iterdir())


def test_ungoverned_builder_runs_violations_and_overspends(tmp_path: Path) -> None:
    config = BuildConfig(attempts=4, budget=9, governed=False, rule=PlaytestRule(seeds=(1,)))
    run = run_build(_ForbiddenBuilder(), config, tmp_path)
    assert all(attempt.executed_violations and attempt.playtests for attempt in run.attempts)
    assert run.account.spent == 12


def test_accepted_quests_audit_from_the_files(tmp_path: Path) -> None:
    policy = BuildPolicy()
    run = run_build(ScriptedBuilder(policy, seed=3), BuildConfig(attempts=3, policy=policy, rule=PlaytestRule(seeds=(1, 2))), tmp_path)
    write_build(run, tmp_path)
    accepted = json.loads((tmp_path / "accepted.json").read_text())
    traced, total, problems = audit_build(tmp_path)
    assert (traced, total, problems) == (len(accepted), len(accepted), [])
    assert run.account.inferences == 0
