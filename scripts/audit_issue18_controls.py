"""Executable negative controls for GitHub issue #18.

These probes attack malformed/hostile model plans and tool outputs and assert
that the current boundary REJECTS them. They complement the defect reproductions
in ``audit_issue18_repro.py``. Run offline:

    .venv/bin/python scripts/audit_issue18_controls.py

Exit 0 means every listed control held (the attack was rejected). A non-zero
exit means a previously enforced boundary regressed. These are not claims of
formal safety; they are bounded, reproducible negative findings.
"""
from __future__ import annotations

import json
from pathlib import Path

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.observations import ObservationContractError, normalize_observation
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.errors import PlanCapabilityValidationError, PlanSchemaValidationError
from arion.intelligence.plan_schema import PlanSchema
from arion.intelligence.plan_validator import PlanValidator


def _rejected(fn, *exceptions):
    try:
        fn()
    except exceptions:
        return True
    except Exception:
        return False
    return False


def _registry():
    reg = CapabilityRegistry()
    reg.register(FilesystemReadCapability(Path.cwd()))
    return reg


def _read_schema(params, **step_extra):
    step = {
        "intent": "read",
        "capability": "filesystem.read",
        "action": "read",
        "params": params,
        "verification": {"policy": "non_empty", "args": {}},
    }
    step.update(step_extra)
    return {"version": "1.0", "intent": "control", "steps": [step]}


def model_forbidden_authority_field():
    return _rejected(
        lambda: PlanSchema.from_dict(_read_schema({"path": "a.txt"}, scope="filesystem:read")),
        PlanSchemaValidationError,
    )


def model_extra_param():
    schema = PlanSchema.from_dict(_read_schema({"path": "a.txt", "shell": "rm -rf /"}))
    return _rejected(lambda: PlanValidator(_registry()).validate(schema), PlanCapabilityValidationError)


def model_omitted_resource():
    schema = PlanSchema.from_dict(_read_schema({}))
    return _rejected(lambda: PlanValidator(_registry()).validate(schema), PlanCapabilityValidationError)


def model_oversized_param():
    return _rejected(
        lambda: PlanSchema.from_dict(_read_schema({"path": "x" * 2001})),
        PlanSchemaValidationError,
    )


def model_deep_nesting():
    nested = "leaf"
    for _ in range(12):
        nested = {"n": nested}
    return _rejected(
        lambda: PlanSchema.from_dict(_read_schema({"path": nested})),
        PlanSchemaValidationError,
    )


def tool_output_non_mapping():
    return _rejected(lambda: normalize_observation([1, 2, 3]), ObservationContractError)


def tool_output_non_string_key():
    return _rejected(lambda: normalize_observation({1: "x"}), ObservationContractError)


def tool_output_cyclic():
    d = {}
    d["self"] = d
    return _rejected(lambda: normalize_observation(d), ObservationContractError)


def tool_output_oversize():
    return _rejected(lambda: normalize_observation({"x": "y" * 100}, max_bytes=50), ObservationContractError)


CONTROLS = {
    "model_forbidden_authority_field": model_forbidden_authority_field,
    "model_extra_param": model_extra_param,
    "model_omitted_resource": model_omitted_resource,
    "model_oversized_param": model_oversized_param,
    "model_deep_nesting": model_deep_nesting,
    "tool_output_non_mapping": tool_output_non_mapping,
    "tool_output_non_string_key": tool_output_non_string_key,
    "tool_output_cyclic": tool_output_cyclic,
    "tool_output_oversize": tool_output_oversize,
}


def main():
    failed = []
    for name, fn in CONTROLS.items():
        ok = fn()
        print(json.dumps({"control": name, "rejected": ok}, sort_keys=True), flush=True)
        if not ok:
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
