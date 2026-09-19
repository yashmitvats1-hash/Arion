"""M9-B.1: Structured filesystem search — bounded, read-only path/filename.

M9-B.2 (ADR-062): the action is declared as an ``ActionSpec``, not a raw
mapping. The declaration is otherwise unchanged from M9-B.1 — same scope, same
resource kind/role, same bounds, same default verification — so runtime
behaviour of ``execute`` is byte-identical; what changes is that the authority
boundary can now READ the action, which is what makes it authorizable,
plannable and verifiable at all.

Note the deliberate interaction between two declarations: ``directory`` is
``required: False`` in ``param_schema`` (so ``execute`` may be called directly
without it and defaults to the sandbox root), yet it IS the declared resource
role — so ``PlanValidator._validate_resource`` and the ADR-009 boundary check
make it effectively MANDATORY for any planned step. That is fail-closed by
design: an implicit "the whole sandbox" resource is exactly what resource-aware
authorization exists to prevent. A plan must name the directory it searches.
"""
from __future__ import annotations
from pathlib import Path

from arion.capabilities.registry import ActionSpec


class FilesystemSearchCapability:
    name = "filesystem.search"
    description = "Bounded structured filesystem search within authorized directory (path/filename only, read-only)."
    actions = [
        ActionSpec(
            name="search",
            description="Search for files/directories by path/filename pattern within directory.",
            required_scope="filesystem:read",
            risk="low",
            side_effects="read_only",
            reversible=True,
            idempotent=True,
            retry_safe=True,
            resource_kind="filesystem:path",
            resource_param="directory",
            param_schema={
                "pattern": {"type": "string", "required": True},
                "directory": {"type": "string", "required": False},
                "max_results": {"type": "integer", "required": False, "max": 100},
            },
            default_verification={"policy": "schema_keys", "args": {"keys": ["results", "count"]}},
        )
    ]

    def __init__(self, sandbox_root: str | Path) -> None:
        self.sandbox_root = Path(sandbox_root).resolve()

    def _resolve_directory(self, directory: str | None) -> Path:
        if directory is None:
            return self.sandbox_root
        return (self.sandbox_root / directory).resolve()

    def execute(self, action: str, params: dict) -> dict:
        if action != "search":
            raise ValueError(f"Unknown action: {action}")
        directory = params.get("directory")
        pattern = params.get("pattern")
        max_results = params.get("max_results", 100)
        if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1 or max_results > 100:
            max_results = 100
        if pattern is None or not isinstance(pattern, str) or len(pattern) > 200 or len(pattern) == 0:
            raise ValueError("pattern must be a non-empty string <= 200 chars")
        target = self._resolve_directory(directory)
        try:
            target.relative_to(self.sandbox_root)
        except ValueError as exc:
            raise ValueError(f"directory outside sandbox: {directory}") from exc
        if not target.exists() or not target.is_dir():
            raise ValueError(f"directory not found: {directory}")
        results = []
        # Fail-closed: resolve every result; exclude symlink-escape or missing
        for p in target.rglob(pattern):
            if len(results) >= max_results:
                break
            try:
                resolved = p.resolve()
                resolved.relative_to(self.sandbox_root)
                try:
                    rel = p.relative_to(target)
                except ValueError:
                    rel = p.name
                results.append({"path": str(rel), "absolute": str(resolved)})
            except ValueError:
                continue
        results.sort(key=lambda r: r["path"])
        return {
            "results": results,
            "count": len(results),
            "directory": str(directory or "."),
            "pattern": pattern,
            "max_results": max_results,
        }
