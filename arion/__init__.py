"""Arion - autonomous personal computing system.

The LLM must never own the agent loop. The orchestrator owns task lifecycle,
state transitions, checkpoints, permissions, execution, verification, recovery,
and completion. The model is an intelligence component called by the
orchestration system.
"""

__version__ = "0.1.0"
