"""Reproducible counterexamples for GitHub issue #18 (audit baseline 8e490ef).

Run offline with the project virtualenv:

    .venv/bin/python scripts/audit_issue18_repro.py
    .venv/bin/python scripts/audit_issue18_repro.py --case write_temp_symlink

Each case uses a disposable sandbox and synthetic data. HTTP cases use
FakeTransport; no provider credentials or external network are used. The
script prints one JSON object per case. ``reproduced: true`` means the
UNDESIRABLE baseline behavior was observed; it is not a product success
claim. After a fix, the corresponding case should report ``reproduced:
false`` (or raise), and the strict-xfail regression tests should be revisited.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from arion.bootstrap import build_engine
from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.http import FakeTransport, HttpGetCapability, HttpResponse
from arion.capabilities.registry import ActionSpec, CapabilityError
from arion.capabilities.write import FilesystemWriteCapability
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.plan_schema import PlanSchema, StructuredStep
from arion.intelligence.providers import openai_compat
from arion.orchestration.authz import (
    ApprovalOutcome,
    AutoApproveHandler,
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.capabilities.http import UrlBoundary
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy

OFFLINE_MODEL_CONFIG = SimpleNamespace(enabled=False, fallback_enabled=False, reflection_enabled=False)
FS = "filesystem:path"


def _policy(*, write=False, origins=None):
    scopes = {"filesystem:read", "git:read"}
    boundaries = {FS: RelativePathBoundary()}
    if write:
        scopes.add("filesystem:write")
    if origins is not None:
        scopes.add("http:get")
        boundaries["url"] = UrlBoundary(origins)
    return ResourcePolicy(
        allowed_scopes=scopes,
        boundaries=boundaries,
        risk_deny=set() if write else {"high"},
        risk_approve={"high"} if write else {"medium"},
    )


class FixedPlanner:
    """Test-only proposer; never registered as a production capability."""

    last_source = "deterministic"

    def __init__(self, steps, required):
        self._steps = steps
        self._required = set(required)
        self.last_transformation = None

    def plan(self, goal_description, task_id, registry, context=None):
        return copy.deepcopy(self._steps)

    def required_capabilities(self, goal_description):
        return set(self._required)


def _write_step(path="report.txt", content="AGENT_PAYLOAD", overwrite=False):
    return PlanStep(
        index=0, intent="write report", capability="filesystem.write", action="write",
        scope="filesystem:write", params={"path": path, "content": content, "overwrite": overwrite},
        verification=VerificationPolicy("non_empty"),  # registry must upgrade this
    )


def _append_step(path="journal.txt", content="A"):
    return PlanStep(
        index=0, intent="append journal", capability="filesystem.append", action="append",
        scope="filesystem:write", params={"path": path, "content": content, "create": False},
        verification=VerificationPolicy("non_empty"),
    )


def _build(root, *, planner=None, policy=None, approval=None):
    return build_engine(
        root / "arion.db", root / "sandbox", planner=planner, policy=policy or _policy(),
        approval_handler=approval, model_config=OFFLINE_MODEL_CONFIG,
    )


def _task(engine, goal_id):
    return engine.goal_manager.task_history(goal_id)[-1]


def _status(value):
    return value.value if hasattr(value, "value") else str(value)


def deterministic_rename(root):
    (root / "sandbox" / "source.txt").write_text("keep me", encoding="utf-8")
    with closing(_build(root)) as engine:
        goal = engine.submit_goal("rename source.txt to target.txt")
        final = engine.run_goal(goal.id)
        task = _task(engine, goal.id)
        ep = engine.memory.get_episode_by_task(task.id)
        ref = engine.memory.get_reflection(ep.reflection_id) if ep and ep.reflection_id else None
        return {
            "reproduced": _status(final.status) == "completed" and not (root / "sandbox" / "target.txt").exists(),
            "goal_status": _status(final.status), "task_status": _status(task.status),
            "executed": [[s.capability, s.action, s.params] for s in task.steps],
            "source_unchanged": (root / "sandbox" / "source.txt").read_text() == "keep me",
            "target_exists": (root / "sandbox" / "target.txt").exists(),
            "episode_outcome": ep.outcome if ep else None,
            "reflection_confidence": ref.confidence if ref else None,
        }


class RenameMisplannerRouter:
    """Hostile/incorrect model proposal: a read for a rename request."""

    def generate(self, prompt, **kwargs):
        raise AssertionError("not used")

    def plan_structured(self, goal, capabilities, context):
        return PlanSchema(version="1.0", intent="rename", steps=[StructuredStep(
            intent="read file", capability="filesystem.read", action="read",
            params={"path": "source.txt"},
            verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
        )])


def model_rename(root):
    (root / "sandbox" / "source.txt").write_text("keep me", encoding="utf-8")
    planner = RealModelPlanner(RenameMisplannerRouter(), fallback_enabled=False)
    with closing(_build(root, planner=planner)) as engine:
        goal = engine.submit_goal("rename source.txt to target.txt")
        final = engine.run_goal(goal.id)
        task = _task(engine, goal.id)
        return {
            "reproduced": _status(final.status) == "completed" and not (root / "sandbox" / "target.txt").exists(),
            "goal_status": _status(final.status), "plan_source": planner.last_source,
            "executed": [[s.capability, s.action] for s in task.steps],
            "target_exists": (root / "sandbox" / "target.txt").exists(),
        }


def memory_skip(root):
    # notes.txt is deliberately absent. A read failure must not become success.
    with closing(_build(root)) as engine:
        goal = engine.submit_goal("read notes.txt")
        engine.run_goal(goal.id)  # first cycle: read fails; caller advances again
        final = engine.run_goal(goal.id)  # next long-horizon cycle consults memory
        tasks = engine.goal_manager.task_history(goal.id)
        last = tasks[-1]
        return {
            "reproduced": _status(final.status) == "completed" and not (root / "sandbox" / "notes.txt").exists(),
            "goal_status": _status(final.status),
            "task_statuses": [_status(t.status) for t in tasks],
            "last_step_statuses": [_status(s.status) for s in last.steps],
            "skipped_reason": last.steps[0].skipped_reason if last.steps else None,
            "capability_executions": sum(e.kind == "capability.executed" for e in engine.storage.list_events(last.id)),
        }


def http_500(root):
    url = "https://allowed.example.invalid/data"
    transport = FakeTransport({url: HttpResponse(status=500, body="upstream failure")})
    with closing(_build(root, policy=_policy(origins={"https://allowed.example.invalid"}))) as engine:
        engine.registry.register(HttpGetCapability(transport=transport))
        goal = engine.submit_goal(f"fetch {url}")
        final = engine.run_goal(goal.id)
        task = _task(engine, goal.id)
        return {
            "reproduced": _status(final.status) == "completed" and task.steps[0].result["status"] == 500,
            "goal_status": _status(final.status), "http_status": task.steps[0].result["status"],
            "verification_policy": task.steps[0].verification.policy,
            "requests": transport.calls,
        }


def git_dotgit_symlink(root):
    outside = root / "outside-metadata"
    (outside / "logs").mkdir(parents=True)
    (outside / "refs" / "heads").mkdir(parents=True)
    (outside / "HEAD").write_text("ref: refs/heads/private\n", encoding="utf-8")
    (outside / "logs" / "HEAD").write_text(
        "0" * 40 + " " + "1" * 40 + " Auditor <audit@example.invalid> 1700000000 +0000\tOUTSIDE_SECRET\n",
        encoding="utf-8",
    )
    (outside / "refs" / "heads" / "private").write_text("1" * 40 + "\n", encoding="utf-8")
    try:
        (root / "sandbox" / ".git").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        return {"reproduced": False, "skipped": f"symlinks unavailable: {exc}"}
    with closing(_build(root)) as engine:
        task = engine.execute_goal("show git history for .")
        result = json.dumps([s.result for s in task.steps])
        return {
            "reproduced": task.status == TaskStatus.COMPLETED and "OUTSIDE_SECRET" in result,
            "task_status": _status(task.status),
            "outside_content_observed": "OUTSIDE_SECRET" in result,
            "actions": [[s.capability, s.action] for s in task.steps],
        }


def write_temp_symlink(root):
    sandbox = root / "sandbox"
    victim = root / "outside-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    try:
        (sandbox / ".report.txt.arion-write-tmp").symlink_to(victim)
    except OSError as exc:
        return {"reproduced": False, "skipped": f"symlinks unavailable: {exc}"}
    planner = FixedPlanner([_write_step()], {"filesystem.write"})
    with closing(_build(root, planner=planner, policy=_policy(write=True), approval=AutoApproveHandler())) as engine:
        goal = engine.submit_goal("write report")
        final = engine.run_goal(goal.id)
        task = _task(engine, goal.id)
        return {
            "reproduced": _status(final.status) == "completed" and victim.read_text() == "AGENT_PAYLOAD",
            "goal_status": _status(final.status), "verification_policy": task.steps[0].verification.policy,
            "outside_before": "ORIGINAL", "outside_after": victim.read_text(),
            "inside_is_symlink": (sandbox / "report.txt").is_symlink(),
        }


def approval_payload_swap(root):
    planner = FixedPlanner([_write_step(path="notes.txt", content="APPROVED_PAYLOAD")], {"filesystem.write"})
    policy = _policy(write=True)
    with closing(_build(root, planner=planner, policy=policy, approval=PendingApprovalHandler())) as engine:
        goal = engine.submit_goal("write notes")
        engine.run_goal(goal.id)
        req = engine.approval_store.list_requests()[0]
        before = copy.deepcopy(req.fingerprint)
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:auditor")
        task = _task(engine, goal.id)
        task.steps[0].params["content"] = "SUBSTITUTED_PAYLOAD"
        engine.storage.save_task(task)
    # Fresh engine: the reused approval is durable, not an in-memory accident.
    with closing(_build(root, planner=planner, policy=policy, approval=PendingApprovalHandler())) as engine:
        final = engine.run_goal(goal.id)
        req = engine.approval_store.get_request(req.approval_id)
        return {
            "reproduced": _status(final.status) == "completed" and (root / "sandbox" / "notes.txt").read_text() == "SUBSTITUTED_PAYLOAD",
            "goal_status": _status(final.status),
            "file_content": (root / "sandbox" / "notes.txt").read_text(),
            "fingerprint_unchanged": before == req.fingerprint,
            "approval_params_keys": req.params_keys,
            "approval_count": len(engine.approval_store.list_requests()),
        }


def redirect_allowlist_mismatch(root):
    a, b = "https://a.example.invalid", "https://b.example.invalid"
    transport = FakeTransport({
        f"{a}/start": HttpResponse(status=302, headers={"location": f"{b}/data"}),
        f"{b}/data": HttpResponse(status=200, body="OFF_POLICY_BODY"),
    })
    with closing(_build(root, policy=_policy(origins={a}))) as engine:
        engine.registry.register(HttpGetCapability(transport=transport, allowed_origins={a, b}))
        goal = engine.submit_goal(f"fetch {a}/start")
        final = engine.run_goal(goal.id)
        task = _task(engine, goal.id)
        return {
            "reproduced": _status(final.status) == "completed" and f"{b}/data" in transport.calls,
            "goal_status": _status(final.status), "requests": transport.calls,
            "direct_b_allowed_by_policy": engine.policy.boundaries["url"].allows(f"{b}/data"),
            "body": task.steps[0].result.get("body"),
        }


def provider_unbounded_read(root, monkeypatch=None):
    calls = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=None):
            calls.append(n)
            return b"x" * 129

    original = openai_compat.urllib.request.urlopen
    openai_compat.urllib.request.urlopen = lambda *a, **k: Resp()
    try:
        router = openai_compat.OpenAICompatModelRouter(api_key="", max_response_bytes=64, max_retries=0)
        rejected = False
        try:
            router._chat({"messages": []})
        except Exception as exc:
            rejected = type(exc).__name__ == "MalformedProviderResponseError"
    finally:
        openai_compat.urllib.request.urlopen = original
    return {"reproduced": rejected and calls == [None], "read_arguments": calls,
            "oversize_rejected_after_read": rejected, "configured_max_bytes": 64}


class CrashAfterAppend(FilesystemAppendCapability):
    def __init__(self, sandbox, signal):
        super().__init__(sandbox)
        self.signal = signal

    def execute(self, action, params):
        result = super().execute(action, params)
        self.signal.write_text("side effect durable; process exiting before outcome save", encoding="utf-8")
        os._exit(86)


def _crash_child(root):
    signal = root / "crash-signal.txt"
    planner = FixedPlanner([_append_step()], {"filesystem.append"})
    engine = _build(root, planner=planner, policy=_policy(write=True), approval=PendingApprovalHandler())
    engine.registry.register(CrashAfterAppend(root / "sandbox", signal))
    goal_id = (root / "goal-id.txt").read_text(encoding="utf-8")
    engine.run_goal(goal_id)
    raise AssertionError("child should have exited inside capability")


def crash_append_duplicate(root):
    sandbox = root / "sandbox"
    (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
    planner = FixedPlanner([_append_step()], {"filesystem.append"})
    policy = _policy(write=True)
    with closing(_build(root, planner=planner, policy=policy, approval=PendingApprovalHandler())) as engine:
        goal = engine.submit_goal("append A once")
        (root / "goal-id.txt").write_text(goal.id, encoding="utf-8")
        engine.run_goal(goal.id)
        req = engine.approval_store.list_requests()[0]
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:auditor")
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--case", "_crash_child", "--workdir", str(root)],
        capture_output=True, text=True, timeout=30,
    )
    after_crash = (sandbox / "journal.txt").read_text()
    if child.returncode != 86 or not (root / "crash-signal.txt").exists():
        return {"reproduced": False, "child_returncode": child.returncode,
                "child_stdout": child.stdout[-1000:], "child_stderr": child.stderr[-1000:]}
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with closing(_build(root, planner=planner, policy=policy, approval=PendingApprovalHandler())) as engine:
        engine.storage.reclaim_expired(now=future)
        # Internal goal-run leases are excluded from the public mutation-lock
        # reclaim sweep; expire this exact lease explicitly in the harness.
        engine.storage.reclaim_expired(now=future, resource_kind="arion:goal-run", resource=goal.id)
        engine.scheduler_registry.reclaim_stale(now=future)
        task_id = _task(engine, goal.id).id
        # This is the public `arion resume <task_id>` path (engine.run_task).
        resumed = engine.run_task(task_id)
        final = engine.run_goal(goal.id)
        task = engine.storage.load_task(task_id)
        recoveries = engine.recovery_store.list_recoveries(goal_id=goal.id)
        attempts = sum(e.kind == "mutation.attempted" for e in engine.storage.list_events(task_id))
        return {
            "reproduced": _status(final.status) == "completed" and after_crash == "BASEA" and (sandbox / "journal.txt").read_text() == "BASEAA",
            "child_returncode": child.returncode, "after_crash": after_crash,
            "after_restart": (sandbox / "journal.txt").read_text(),
            "goal_status": _status(final.status), "resumed_task_status": _status(resumed.status),
            "task_status": _status(task.status),
            "append_attempts_total": attempts,
            "recovery_records": len(recoveries),
        }


def declaration_taxonomy_fail_open(root):
    """Unknown risk/side-effects strings are accepted and fail open."""

    class UnknownRiskWrite:
        name = "probe.unknown_risk"
        description = "test-only unknown-risk mutation"
        actions = [ActionSpec(
            name="write", description="test", required_scope="filesystem:write",
            risk="critical",  # not in the documented none/low/medium/high vocabulary
            side_effects="mutating", retry_safe=False,
            resource_kind=FS, resource_param="path",
            param_schema={"path": {"type": "string", "required": True}},
            default_verification={"policy": "non_empty"},
        )]

        def __init__(self, sandbox):
            self.sandbox = sandbox

        def execute(self, action, params):
            (self.sandbox / params["path"]).write_text("unknown-risk", encoding="utf-8")
            return {"ok": True}

    class UnknownSideEffectWrite:
        name = "probe.unknown_side_effect"
        description = "test-only unknown-side-effect mutation"
        actions = [ActionSpec(
            name="write", description="test", required_scope="filesystem:read",
            risk="low", side_effects="Mutating",  # capitalization outside taxonomy
            retry_safe=False, resource_kind=FS, resource_param="path",
            param_schema={"path": {"type": "string", "required": True}},
            default_verification={"policy": "non_empty"},
        )]

        def __init__(self, sandbox):
            self.sandbox = sandbox

        def execute(self, action, params):
            (self.sandbox / params["path"]).write_text("unknown-side-effect", encoding="utf-8")
            return {"ok": True}

    sandbox = root / "sandbox"
    risk_step = PlanStep(index=0, intent="unknown risk", capability="probe.unknown_risk", action="write",
                         scope="filesystem:write", params={"path": "risk.txt"}, verification=VerificationPolicy("non_empty"))
    side_step = PlanStep(index=0, intent="unknown side effect", capability="probe.unknown_side_effect", action="write",
                         scope="filesystem:read", params={"path": "side.txt"}, verification=VerificationPolicy("non_empty"))
    with closing(_build(root, planner=FixedPlanner([risk_step], {"probe.unknown_risk"}),
                        policy=_policy(write=True), approval=PendingApprovalHandler())) as engine:
        engine.registry.register(UnknownRiskWrite(sandbox))
        goal = engine.submit_goal("unknown risk probe")
        final = engine.run_goal(goal.id)
        risk_completed = _status(final.status) == "completed" and (sandbox / "risk.txt").exists()
        risk_approvals = len(engine.approval_store.list_requests())
    with closing(_build(root, planner=FixedPlanner([side_step], {"probe.unknown_side_effect"}))) as engine:
        engine.registry.register(UnknownSideEffectWrite(sandbox))
        goal = engine.submit_goal("unknown side effect probe")
        final = engine.run_goal(goal.id)
        task = _task(engine, goal.id)
        side_completed = _status(final.status) == "completed" and (sandbox / "side.txt").exists()
        mutation_events = sum(e.kind == "mutation.attempted" for e in engine.storage.list_events(task.id))
    return {
        "reproduced": risk_completed and risk_approvals == 0 and side_completed and mutation_events == 0,
        "unknown_risk_completed_without_approval": risk_completed and risk_approvals == 0,
        "unknown_side_effect_completed_without_mutation_lock": side_completed and mutation_events == 0,
    }


CASES = {
    "deterministic_rename": deterministic_rename,
    "model_rename": model_rename,
    "memory_skip": memory_skip,
    "http_500": http_500,
    "git_dotgit_symlink": git_dotgit_symlink,
    "write_temp_symlink": write_temp_symlink,
    "approval_payload_swap": approval_payload_swap,
    "redirect_allowlist_mismatch": redirect_allowlist_mismatch,
    "provider_unbounded_read": provider_unbounded_read,
    "crash_append_duplicate": crash_append_duplicate,
    "declaration_taxonomy_fail_open": declaration_taxonomy_fail_open,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[*CASES, "_crash_child"], action="append")
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    selected = args.case or list(CASES)
    if "_crash_child" in selected:
        if args.workdir is None:
            parser.error("_crash_child requires --workdir")
        _crash_child(args.workdir)
        return 0
    failed = []
    for name in selected:
        if args.workdir is not None:
            root = args.workdir / name
            (root / "sandbox").mkdir(parents=True, exist_ok=True)
            result = CASES[name](root)
        else:
            with tempfile.TemporaryDirectory(prefix=f"arion18-{name}-") as td:
                root = Path(td)
                (root / "sandbox").mkdir()
                result = CASES[name](root)
        print(json.dumps({"case": name, **result}, sort_keys=True), flush=True)
        if not result.get("reproduced"):
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
