"""Provider smoke tests - harness B (ADR-057 M5 decision 1).

Tier 1 - scripted local-server scenarios (OFFLINE, always run):
    a local ThreadingHTTPServer plays the OpenAI-compatible
    ``/chat/completions`` endpoint, so the COMPLETE response path
    (HTTP transport -> JSON -> PlanSchema -> PlanValidator -> PlanSteps)
    is exercised with ZERO external credentials and no network access
    beyond loopback. The engine is wired through the M5 env-driven
    runtime path (``build_engine`` + ``ARION_LLM_*``).

    Scenarios: provider down (connection refused), malformed response,
    provider failure with deterministic fallback, and strict mode
    (``ARION_LLM_FALLBACK=0``) failing closed.

Tier 2 - live-provider scenario (externally gated, ADR-011 legacy):
    when ``ARION_LLM_BASE_URL`` / ``ARION_LLM_API_KEY`` are configured,
    the same env-driven runtime path is exercised against that real
    endpoint. Skips cleanly otherwise - normal test runs never require
    credentials and never touch the network beyond loopback.

    Run explicitly, e.g.:
        ARION_LLM_BASE_URL=http://127.0.0.1:8971/v1 ARION_LLM_MODEL=mock \
            pytest tests/smoke/test_live_provider.py -v

No secrets are committed; the key is read from the environment only.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION

pytestmark = pytest.mark.smoke

VALID_PLAN_CONTENT = json.dumps({
    "version": PLAN_SCHEMA_VERSION,
    "intent": "Read the README.md file",
    "steps": [
        {"intent": "read readme", "capability": "filesystem.read",
         "action": "read", "params": {"path": "README.md"},
         "verification": {"policy": "schema_keys",
                          "args": {"keys": ["content"]}}},
    ],
})

MALFORMED_PLAN_CONTENT = "{not valid json"


class _ScriptedProvider:
    """Offline OpenAI-compatible /chat/completions endpoint.

    Serves the full adapter HTTP contract locally; `stop()` closes the
    port so the 'provider down' scenario is a genuine connection refusal.
    """

    def __init__(self, plan_content=VALID_PLAN_CONTENT, status=200):
        self.plan_content = plan_content
        self.status = status
        self._httpd = None

    def start(self) -> str:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                if provider.status != 200:
                    self.send_response(provider.status)
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                payload = json.dumps(
                    {"choices": [{"message": {
                        "content": provider.plan_content}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence the dev server log
                pass

        return Handler


# ---------------------------------------------------------------- helpers


def _clear_llm_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("ARION_LLM")]:
        monkeypatch.delenv(key, raising=False)


def _set_llm_env(monkeypatch, **kw):
    _clear_llm_env(monkeypatch)
    defaults = dict(
        ARION_LLM_PROVIDER="openai-compatible",
        ARION_LLM_MODEL="mock",
        ARION_LLM_BASE_URL="http://127.0.0.1:1",
        ARION_LLM_API_KEY="sk-test",
        ARION_LLM_TIMEOUT_SECONDS="2",
        ARION_LLM_MAX_RETRIES="0",
    )
    defaults.update(kw)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)


def _sandbox(tmp_path):
    sb = tmp_path / "repo"
    sb.mkdir(exist_ok=True)
    (sb / "README.md").write_text("# Smoke\n", encoding="utf-8")
    return sb


# Tier-1 scenarios each call `_set_llm_env`, which clears any ambient
# ARION_LLM_* first, so a configured sandbox can never leak into the
# scripted assertions. The tier-2 live scenario intentionally leaves the
# real environment untouched.

# ============================================= tier 1: scripted (offline)


def test_scripted_provider_full_model_path(tmp_path, monkeypatch):
    """200 + valid plan: the env-driven runtime consults the provider and
    the full path provider -> JSON -> PlanSchema -> PlanValidator ->
    PlanSteps completes the task."""
    from arion.bootstrap import build_engine
    from arion.state.models import TaskStatus

    provider = _ScriptedProvider()
    base = provider.start()
    try:
        _set_llm_env(monkeypatch, ARION_LLM_BASE_URL=base)
        engine = build_engine(str(tmp_path / "t.db"),
                              _sandbox(tmp_path), memory=True)
        try:
            task = engine.execute_goal("Read the README.md file")
            kinds = [e.kind for e in engine.storage.list_events(task.id)]
            assert "model.response.received" in kinds, \
                f"provider was not consulted; events: {kinds}"
            assert "plan.validation.passed" in kinds, \
                f"plan never validated; events: {kinds}"
            assert task.status == TaskStatus.COMPLETED, \
                f"task did not complete: {task.status} {task.error}"
            assert task.steps and "content" in task.steps[0].result
        finally:
            engine.shutdown()
    finally:
        provider.stop()


def test_scripted_provider_malformed_response_falls_back(tmp_path, monkeypatch):
    """200 + malformed plan content: validation fails, the deterministic
    fallback produces the plan (model proposes, Arion decides)."""
    from arion.bootstrap import build_engine
    from arion.state.models import TaskStatus

    provider = _ScriptedProvider(plan_content=MALFORMED_PLAN_CONTENT)
    base = provider.start()
    try:
        _set_llm_env(monkeypatch, ARION_LLM_BASE_URL=base)
        engine = build_engine(str(tmp_path / "t.db"),
                              _sandbox(tmp_path), memory=True)
        try:
            task = engine.execute_goal("Read the README.md file")
            assert task.status == TaskStatus.COMPLETED, \
                f"task did not complete: {task.status} {task.error}"
            kinds = [e.kind for e in engine.storage.list_events(task.id)]
            assert "plan.validation.failed" in kinds, \
                f"malformed response was not rejected; events: {kinds}"
            assert "model.fallback" in kinds, \
                f"deterministic fallback did not run; events: {kinds}"
            produced = [e for e in engine.storage.list_events(task.id)
                        if e.kind == "plan.produced"][0]
            assert produced.detail["source"] == "deterministic"
            assert task.steps and "content" in task.steps[0].result
        finally:
            engine.shutdown()
    finally:
        provider.stop()


def test_scripted_provider_down_falls_back(tmp_path, monkeypatch):
    """Provider unreachable (connection refused): typed ProviderUnavailable
    path falls back to the deterministic planner and the task completes."""
    from arion.bootstrap import build_engine
    from arion.state.models import TaskStatus

    provider = _ScriptedProvider()
    base = provider.start()
    provider.stop()            # close the port: genuine connection refusal
    _set_llm_env(monkeypatch, ARION_LLM_BASE_URL=base)
    engine = build_engine(str(tmp_path / "t.db"), _sandbox(tmp_path),
                          memory=True)
    try:
        task = engine.execute_goal("Read the README.md file")
        assert task.status == TaskStatus.COMPLETED, \
            f"task did not complete: {task.status} {task.error}"
        kinds = [e.kind for e in engine.storage.list_events(task.id)]
        assert "model.fallback" in kinds, \
            f"provider-down did not fall back; events: {kinds}"
        produced = [e for e in engine.storage.list_events(task.id)
                    if e.kind == "plan.produced"][0]
        assert produced.detail["source"] == "deterministic"
        assert task.steps and "content" in task.steps[0].result
    finally:
        engine.shutdown()


def test_scripted_provider_strict_mode_fails_closed(tmp_path, monkeypatch):
    """ARION_LLM_FALLBACK=0 + provider 500: strict typed durable failure,
    NO deterministic fallback and NO capability execution."""
    from arion.bootstrap import build_engine
    from arion.state.models import TaskStatus

    provider = _ScriptedProvider(status=500)
    base = provider.start()
    try:
        _set_llm_env(monkeypatch, ARION_LLM_BASE_URL=base,
                     ARION_LLM_FALLBACK="0")
        engine = build_engine(str(tmp_path / "t.db"),
                              _sandbox(tmp_path), memory=True)
        try:
            task = engine.execute_goal("Read the README.md file")
            assert task.status == TaskStatus.FAILED
            kinds = [e.kind for e in engine.storage.list_events(task.id)]
            assert "model.fallback" not in kinds, \
                f"strict mode must not fall back; events: {kinds}"
            assert "capability.executed" not in kinds, \
                f"nothing may execute in strict mode; events: {kinds}"
        finally:
            engine.shutdown()
    finally:
        provider.stop()


# ===================================== tier 2: live provider (env-gated)


def test_live_provider_full_path(tmp_path, monkeypatch):
    if not (os.environ.get("ARION_LLM_BASE_URL")
            or os.environ.get("ARION_LLM_API_KEY")):
        pytest.skip(
            "ARION_LLM_BASE_URL / ARION_LLM_API_KEY not set - live-provider "
            "smoke test skipped. Set ARION_LLM_BASE_URL (e.g. to a local "
            "OpenAI-compatible endpoint) to run it."
        )

    # verify network access to the endpoint BEFORE reporting a pass/fail
    base = os.environ["ARION_LLM_BASE_URL"].rstrip("/")
    try:
        req = urllib.request.Request(base)  # some servers 404; that proves reachability
        urllib.request.urlopen(req, timeout=5).read()
    except urllib.error.HTTPError:
        pass  # a 4xx/5xx still proves the endpoint is reachable over HTTP
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"provider endpoint {base!r} is not reachable: {exc}")

    from arion.bootstrap import build_engine
    from arion.intelligence.model_planner import RealModelPlanner
    from arion.state.models import TaskStatus

    # Complete the config surface for the env-driven runtime while keeping
    # the legacy gating contract (BASE_URL / API_KEY): the single registered
    # provider name defaults to "openai-compatible" and the model name to the
    # legacy router default when the operator did not set them explicitly.
    monkeypatch.setenv("ARION_LLM_PROVIDER",
                       os.environ.get("ARION_LLM_PROVIDER", "openai-compatible"))
    monkeypatch.setenv("ARION_LLM_MODEL",
                       os.environ.get("ARION_LLM_MODEL", "gpt-4o-mini"))
    engine = build_engine(str(tmp_path / "t.db"), _sandbox(tmp_path),
                          memory=True)
    try:
        assert isinstance(engine.planner, RealModelPlanner), \
            "ARION_LLM_* configured but the env-driven runtime did not " \
            "select the model planner"
        # Full path: provider -> JSON -> PlanSchema -> PlanValidator -> PlanSteps
        task = engine.execute_goal("Read the README.md file in this repository.")
        kinds = [e.kind for e in engine.storage.list_events(task.id)]
        assert "model.response.received" in kinds, \
            f"provider was not consulted; events: {kinds}"
        assert "plan.validation.passed" in kinds, \
            f"plan never validated; events: {kinds}"
        assert task.status == TaskStatus.COMPLETED, \
            f"task did not complete: {task.status} {task.error}"
        assert task.steps and "content" in task.steps[0].result
    finally:
        engine.shutdown()
