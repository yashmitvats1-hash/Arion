# M9-B.1 Completion — Structured Filesystem Search

- Capability: `filesystem.search` (action: `search`)
- Scope: `filesystem:read` (existing)
- Resource model: `directory` (`filesystem:path`), verified via `RelativePathBoundary`
- Verification: `schema_keys` with `keys: ["results", "count"]` — validates structure; zero matches (`count==0`) is legitimate success (not `non_empty`)
- Bound: `max_results` capped at 100 (hard); `pattern` bounded at 200 chars (PlanSchema); result paths verified via `_resolve_inside` (fail-closed for symlink escape)
- Determinism: sorted by relative path; duplicate paths deduplicated implicitly by `resolve()` + sorted result
- Security: search result is DATA, never authorization; subsequent read/write/append receive independent authorization against `directory`/`path`
- No changes to `authz.py`, `engine.py`, `PlanValidator`, `ResolvedResource` required.
- Tests: `tests/test_search.py` (8 tests) — happy, empty, nested, boundary, symlink, determinism, authorization.
- Full suite: PASS (exit 0) at HEAD `38c4574`.
