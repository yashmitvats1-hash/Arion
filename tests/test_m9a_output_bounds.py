"""M9-A Step 3: Model-output bounds (ADR-057 D2 / M2)."""
from __future__ import annotations
import pytest
from arion.intelligence.plan_schema import PlanSchema, MAX_PLAN_STEPS, MAX_STEP_STRING


def test_output_bounds_enforced_by_schema():
    big = {"version":"1.0","intent":"x","steps":[{"intent":"x","capability":"c","action":"a","params":{},"verification":{"policy":"non_empty","args":{}}}]*101}
    with pytest.raises(Exception):
        PlanSchema.from_dict(big)
    long_str = "x" * (MAX_STEP_STRING + 1)
    bad = {"version":"1.0","intent":"x","steps":[{"intent":long_str,"capability":"c","action":"a","params":{},"verification":{"policy":"non_empty","args":{}}}]}
    with pytest.raises(Exception):
        PlanSchema.from_dict(bad)
