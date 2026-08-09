"""CLI interface tests: run, status, tasks, events, resume, capabilities."""

from arion.interfaces.cli import main


def _run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, out


def _task_id(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("task_id: "):
            return line.split("task_id: ")[1].strip()
    raise AssertionError(f"no task_id line in output:\n{out}")


def test_cli_run_completes_goal(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc, out = _run(["run", "summarize this repository", "--db", str(tmp_path / "arion.db")], capsys)
    assert rc == 0
    assert "goal executed" in out
    assert "status: completed" in out
    assert "[ok] step 0" in out
    assert "[ok] step 1" in out


def test_cli_tasks_and_status(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    rc, out = _run(["run", "summarize this repository", "--db", db], capsys)
    task_id = _task_id(out)
    assert task_id.startswith("task_")

    rc, out = _run(["tasks", "--db", db], capsys)
    assert task_id in out
    assert "completed" in out

    rc, out = _run(["status", task_id, "--db", db], capsys)
    assert "COMPLETED" in out.upper()
    assert "summarize this repository" in out


def test_cli_events_trail(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    rc, out = _run(["run", "summarize this repository", "--db", db], capsys)
    task_id = _task_id(out)

    rc, out = _run(["events", "--task", task_id, "--db", db], capsys)
    assert "permission.checked" in out
    assert "verification.passed" in out
    assert "checkpoint.persisted" in out
    assert "task.completed" in out


def test_cli_resume_completed_task_is_idempotent(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    rc, out = _run(["run", "summarize this repository", "--db", db], capsys)
    task_id = _task_id(out)

    rc, out = _run(["resume", task_id, "--db", db], capsys)
    assert rc == 0
    assert "status: completed" in out


def test_cli_capabilities(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc, out = _run(["capabilities", "--db", str(tmp_path / "arion.db")], capsys)
    assert "filesystem.read" in out
    assert "filesystem:read" in out
