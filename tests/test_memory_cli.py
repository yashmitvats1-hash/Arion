"""Memory CLI tests (learning milestone): arion memory episodes|reflections|
search|stats|consolidate."""

from arion.bootstrap import build_engine
from arion.interfaces.cli import main


def _seed(tmp_path):
    """Run a goal so an episode + reflection exist."""
    import os

    sandbox = tmp_path / "sandbox"
    os.makedirs(sandbox, exist_ok=True)
    with open(sandbox / "README.md", "w") as fh:
        fh.write("# seed\n")
    engine = build_engine(str(tmp_path / "m.db"), sandbox_root=str(sandbox))
    engine.execute_goal("summarize this repository")
    engine.storage.close()
    return str(tmp_path / "m.db")


def _run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, out


def test_cli_memory_episodes(tmp_path, capsys):
    db = _seed(tmp_path)
    rc, out = _run(["memory", "episodes", "--db", db], capsys)
    assert rc == 0
    assert "ep_" in out
    assert "completed" in out


def test_cli_memory_episodes_json(tmp_path, capsys):
    db = _seed(tmp_path)
    rc, out = _run(["memory", "episodes", "--db", db, "--json"], capsys)
    assert rc == 0
    import json

    data = json.loads(out)
    assert isinstance(data, list) and data
    assert data[0]["episode_id"].startswith("ep_")
    assert "goal" in data[0]


def test_cli_memory_reflections(tmp_path, capsys):
    db = _seed(tmp_path)
    rc, out = _run(["memory", "reflections", "--db", db], capsys)
    assert rc == 0
    assert "refl_" in out
    assert "lesson" in out.lower() or "refl_" in out


def test_cli_memory_search(tmp_path, capsys):
    db = _seed(tmp_path)
    rc, out = _run(["memory", "search", "summarize", "--db", db], capsys)
    assert rc == 0
    assert "ep_" in out


def test_cli_memory_stats(tmp_path, capsys):
    db = _seed(tmp_path)
    rc, out = _run(["memory", "stats", "--db", db], capsys)
    assert rc == 0
    assert "episodes:" in out
    assert "reflections:" in out


def test_cli_memory_stats_json(tmp_path, capsys):
    db = _seed(tmp_path)
    rc, out = _run(["memory", "stats", "--db", db, "--json"], capsys)
    assert rc == 0
    import json

    stats = json.loads(out)
    assert stats["episodes"] >= 1
    assert stats["reflections"] >= 1


def test_cli_memory_consolidate(tmp_path, capsys):
    db = _seed(tmp_path)
    rc, out = _run(["memory", "consolidate", "--db", db], capsys)
    assert rc == 0
    assert "consolidation record(s) created" in out


def test_cli_memory_does_not_expose_secrets(tmp_path, capsys):
    """The CLI never prints param values or raw transcripts."""
    db = _seed(tmp_path)
    for argv in (["memory", "episodes", "--db", db, "--json"],
                 ["memory", "reflections", "--db", db, "--json"],
                 ["memory", "stats", "--db", db, "--json"]):
        rc, out = _run(argv, capsys)
        assert rc == 0
        assert "ARION_LLM_API_KEY" not in out
