"""StrategySelector rule 1: dotted-token capability detection (ADR-017).

Regression tests for the baseline-hygiene fix: ordinary dotted tokens in goal
descriptions (file names such as README.md / package.json, dotfiles, version
strings) must never be misread as missing capability names, while genuine
unregistered capability mentions (filesystem.write, http.get, web.search)
still select `blocked_missing_capability`.
"""

import pytest

from arion.cognition.strategy import StrategySelector

# Full registry as observed by bootstrap's WorldStateMonitor
# (world_monitor.observe("registered_capabilities", sorted(registry.list()))).
FULL_REGISTRY = {
    "registered_capabilities": {
        "value": [
            "filesystem.append",
            "filesystem.read",
            "filesystem.write",
            "git.log",
            "http.get",
        ]
    }
}

# A registry that lacks the mutating/network capabilities.
READ_ONLY_REGISTRY = {
    "registered_capabilities": {"value": ["filesystem.read"]}
}


def _select(goal, env):
    return StrategySelector().select(goal, [], env, [])


@pytest.mark.parametrize(
    "goal",
    [
        "summarize the README.md",
        "read package.json and report the scripts",
        "inspect pyproject.toml for dependencies",
        "check notes.log for errors",
        "review docs/design.md",
        "list the requirements.txt contents",
        "open config.yaml",
        "show .env.example",
    ],
)
def test_file_tokens_are_not_missing_capabilities(goal):
    strat = _select(goal, FULL_REGISTRY)
    assert strat.name != "blocked_missing_capability"
    assert "missing_capabilities" not in strat.constraints


@pytest.mark.parametrize(
    "goal",
    [
        "upgrade the tool to v1.2.3",
        "compare versions 2.0.1 and 2.1.0",
        "the .gitignore is missing",
    ],
)
def test_version_and_dotfile_tokens_are_not_capabilities(goal):
    strat = _select(goal, FULL_REGISTRY)
    assert strat.name != "blocked_missing_capability"


@pytest.mark.parametrize(
    "goal,missing",
    [
        ("use filesystem.write to save the file", ["filesystem.write"]),
        ("inspect http.get repo", ["http.get"]),
        ("use web.search to look things up", ["web.search"]),
        ("invoke browser.automation to open the site", ["browser.automation"]),
    ],
)
def test_unregistered_capability_tokens_still_block(goal, missing):
    strat = _select(goal, READ_ONLY_REGISTRY)
    assert strat.name == "blocked_missing_capability"
    assert strat.constraints["missing_capabilities"] == missing


@pytest.mark.parametrize(
    "goal",
    [
        "use filesystem.write to save the file",
        "fetch http.get data",
        "inspect git.log history",
    ],
)
def test_registered_capability_mentions_are_not_missing(goal):
    # A registered capability is never "missing" even though the token is
    # capability-shaped (missing = needed - registered).
    strat = _select(goal, FULL_REGISTRY)
    assert strat.name != "blocked_missing_capability"


def test_missing_capabilities_sorted_deterministic():
    env = {"registered_capabilities": {"value": ["filesystem.read"]}}
    goal = "use filesystem.write and http.get together"
    results = {
        tuple(_select(goal, env).constraints["missing_capabilities"])
        for _ in range(5)
    }
    # Stable, sorted order across repeated selections (set-iteration order is
    # hash-dependent, so the list must be sorted to be deterministic).
    assert results == {("filesystem.write", "http.get")}
