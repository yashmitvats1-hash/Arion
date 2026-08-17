"""Plan schema tests (ADR-011): versioned, strict, serializable.

Covers the structural side of the adversarial matrix:
- valid structured plan
- malformed JSON
- invalid schema (version, missing/unknown fields, bad types, ordering)
- scope/resource-kind/resource-parameter spoofing rejected at the schema gate
- invalid verification policy
- serialization / persistence round-trips
"""

import pytest

from arion.intelligence.plan_schema import (
    PLAN_SCHEMA_VERSION,
    PlanSchema,
    PlanValidationError,
    StructuredStep,
)

VALID = {
    "version": PLAN_SCHEMA_VERSION,
    "intent": "Inspect this repository",
    "steps": [
        {
            "intent": "list root",
            "capability": "filesystem.read",
            "action": "list",
            "params": {"path": "."},
            "verification": {"policy": "non_empty"},
        },
        {
            "intent": "read key file",
            "capability": "filesystem.read",
            "action": "read",
            "params": {"path": "README.md"},
            "verification": {"policy": "schema_keys", "args": {"keys": ["content"]}},
            "depends_on": [0],
        },
    ],
}


def _from(d: dict):
    return PlanSchema.from_dict(d)


def test_valid_structured_plan():
    schema = _from(VALID)
    assert schema.version == PLAN_SCHEMA_VERSION
    assert schema.intent == "Inspect this repository"
    assert len(schema.steps) == 2
    assert schema.steps[0].capability == "filesystem.read"
    assert schema.steps[0].params == {"path": "."}
    assert schema.steps[1].depends_on == [0]


def test_malformed_json_rejected():
    with pytest.raises(PlanValidationError):
        PlanSchema.from_json("{not json")
    with pytest.raises(PlanValidationError):
        PlanSchema.from_json('"just a string"')


def test_non_object_plan_rejected():
    for bad in ([], "text", 42, None):
        with pytest.raises(PlanValidationError):
            _from(bad)


def test_wrong_version_rejected():
    d = dict(VALID, version="9.9")
    with pytest.raises(PlanValidationError, match="version"):
        _from(d)


def test_missing_version_rejected():
    d = {k: v for k, v in VALID.items() if k != "version"}
    with pytest.raises(PlanValidationError, match="version"):
        _from(d)


def test_missing_intent_rejected():
    d = dict(VALID, intent="")
    with pytest.raises(PlanValidationError, match="intent"):
        _from(d)


def test_missing_steps_rejected():
    with pytest.raises(PlanValidationError, match="steps"):
        _from({"version": PLAN_SCHEMA_VERSION, "intent": "x"})
    with pytest.raises(PlanValidationError, match="steps"):
        _from({"version": PLAN_SCHEMA_VERSION, "intent": "x", "steps": []})


def test_unknown_top_level_field_rejected():
    with pytest.raises(PlanValidationError, match="top-level"):
        _from(dict(VALID, extra="x"))


def test_unknown_step_field_rejected():
    d = _deepcopy_steps()
    d["steps"][0]["flavor"] = "chocolate"
    with pytest.raises(PlanValidationError, match="unknown field"):
        _from(d)


def test_step_requires_intent_capability_action():
    for field in ("intent", "capability", "action"):
        d = _deepcopy_steps()
        del d["steps"][0][field]
        with pytest.raises(PlanValidationError, match=field):
            _from(d)
        d2 = _deepcopy_steps()
        d2["steps"][0][field] = 123
        with pytest.raises(PlanValidationError, match=field):
            _from(d2)


def test_params_must_be_object():
    d = _deepcopy_steps()
    d["steps"][0]["params"] = ["path"]
    with pytest.raises(PlanValidationError, match="params"):
        _from(d)


def test_invalid_verification_policy_rejected():
    d = _deepcopy_steps()
    d["steps"][0]["verification"] = {"policy": "ask_the_llm"}
    with pytest.raises(PlanValidationError, match="verification"):
        _from(d)


def test_schema_keys_verification_requires_keys_list():
    d = _deepcopy_steps()
    d["steps"][0]["verification"] = {"policy": "schema_keys", "args": {"keys": "content"}}
    with pytest.raises(PlanValidationError, match="schema_keys"):
        _from(d)


def test_verification_required():
    d = _deepcopy_steps()
    del d["steps"][0]["verification"]
    with pytest.raises(PlanValidationError, match="verification"):
        _from(d)


def test_depends_on_forward_reference_rejected():
    d = _deepcopy_steps()
    d["steps"][0]["depends_on"] = [1]  # step 0 cannot depend on step 1
    with pytest.raises(PlanValidationError, match="depends_on"):
        _from(d)


def test_depends_on_duplicates_rejected():
    d = _deepcopy_steps()
    d["steps"][1]["depends_on"] = [0, 0]
    with pytest.raises(PlanValidationError, match="depends_on"):
        _from(d)


# ---- adversarial: the model must never set authorization fields ----


def test_scope_spoofing_rejected():
    d = _deepcopy_steps()
    d["steps"][0]["scope"] = "shell:exec"
    with pytest.raises(PlanValidationError, match="cannot set field"):
        _from(d)


def test_resource_kind_spoofing_rejected():
    d = _deepcopy_steps()
    d["steps"][0]["resource_kind"] = "filesystem:write"
    with pytest.raises(PlanValidationError, match="cannot set field"):
        _from(d)


def test_resource_param_spoofing_rejected():
    d = _deepcopy_steps()
    d["steps"][0]["resource_param"] = "anything"
    with pytest.raises(PlanValidationError, match="cannot set field"):
        _from(d)


def test_risk_and_permission_fields_rejected():
    for field in ("risk", "side_effects", "permissions", "actor", "approve", "grant", "authorization"):
        d = _deepcopy_steps()
        d["steps"][0][field] = "x"
        with pytest.raises(PlanValidationError, match="cannot set field"):
            _from(d)


def test_reserved_param_keys_rejected():
    d = _deepcopy_steps()
    d["steps"][0]["params"] = {"path": ".", "scope": "shell:exec"}
    with pytest.raises(PlanValidationError, match="reserved"):
        _from(d)


def test_serialization_round_trip():
    schema = _from(VALID)
    json_text = schema.to_json()
    schema2 = PlanSchema.from_json(json_text)
    assert schema2 == schema
    assert schema2.to_dict() == schema.to_dict()


def test_steps_order_is_positional():
    schema = _from(VALID)
    for i, s in enumerate(schema.steps):
        assert s == schema.steps[i]


def _deepcopy_steps():
    import copy

    return copy.deepcopy(VALID)


def test_structured_step_requires_object():
    with pytest.raises(PlanValidationError):
        StructuredStep.from_dict("not a dict", 0)
