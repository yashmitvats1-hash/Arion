"""Live-provider smoke test (ADR-011).

Validates the COMPLETE response path against a real OpenAI-compatible HTTP
endpoint when explicitly enabled:

    provider (HTTP) -> JSON -> PlanSchema -> PlanValidator -> PlanSteps

- Configured via the same environment variables the adapter uses:
  ARION_LLM_BASE_URL, ARION_LLM_API_KEY (optional for local endpoints),
  ARION_LLM_MODEL.
- Skips cleanly when provider configuration is absent - normal test runs
  never require credentials and never touch the network.
- No secrets are committed; the key is read from the environment only.

Run explicitly, e.g.:
    ARION_LLM_BASE_URL=http://127.0.0.1:8971/v1 ARION_LLM_MODEL=mock \
        pytest tests/smoke/test_live_provider.py -v

A local OpenAI-compatible endpoint (Ollama, LiteLLM, vLLM, ...) is acceptable.
"""

import os
import urllib.request

import pytest

pytestmark = pytest.mark.smoke


def _configured() -> bool:
    return bool(os.environ.get("ARION_LLM_BASE_URL") or os.environ.get("ARION_LLM_API_KEY"))


def test_live_provider_full_path(tmp_path):
    if not _configured():
        pytest.skip(
            "ARION_LLM_BASE_URL / ARION_LLM_API_KEY not set - live-provider smoke test skipped. "
            "Set ARION_LLM_BASE_URL (e.g. to a local OpenAI-compatible endpoint) to run it."
        )

    # verify network access to the endpoint BEFORE reporting a pass/fail
    base = os.environ["ARION_LLM_BASE_URL"].rstrip("/")
    try:
        req = urllib.request.Request(base)  # GET to the base - some servers 404; that proves reachability
        urllib.request.urlopen(req, timeout=5).read()
    except urllib.error.HTTPError:
        pass  # a 4xx/5xx still proves the endpoint is reachable over HTTP
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"provider endpoint {base!r} is not reachable: {exc}")

    from arion.capabilities.filesystem import FilesystemReadCapability
    from arion.capabilities.registry import CapabilityRegistry
    from arion.intelligence.model_planner import RealModelPlanner
    from arion.intelligence.providers import OpenAICompatModelRouter
    from arion.observability.events import EventLogger
    from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
    from arion.orchestration.engine import ArionEngine
    from arion.state.models import TaskStatus
    from arion.state.store import SQLiteStorage

    sandbox = tmp_path / "repo"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("# Live smoke\n", encoding="utf-8")

    storage = SQLiteStorage(tmp_path / "arion.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    router = OpenAICompatModelRouter(
        model=os.environ.get("ARION_LLM_MODEL", "gpt-4o-mini"),
        sink=events,
    )
    planner = RealModelPlanner(router, events=events)
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=router,
        events=events,
        policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
    )

    # Full path: provider -> JSON -> PlanSchema -> PlanValidator -> PlanSteps
    task = engine.execute_goal("Read the README.md file in this repository.")

    kinds = [e.kind for e in storage.list_events(task.id)]
    assert "model.response.received" in kinds, f"provider was not consulted; events: {kinds}"
    assert "plan.validation.passed" in kinds, f"plan never validated; events: {kinds}"
    assert task.status == TaskStatus.COMPLETED, f"task did not complete: {task.status} {task.error}"
    assert task.steps and "content" in task.steps[0].result
