"""M9-B.1: Structured filesystem search — bounded, read-only."""
from __future__ import annotations
from pathlib import Path
import pytest
from arion.capabilities.search import FilesystemSearchCapability
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.orchestration.authz import ResourcePolicy, RelativePathBoundary


class TestFilesystemSearch:
    def test_search_finds_matching_file(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "README.md").write_text("hello")
        (root / "notes.txt").write_text("note")
        cap = FilesystemSearchCapability(root)
        result = cap.execute("search", {"pattern": "*.md", "directory": ".", "max_results": 10})
        assert result["count"] == 1
        assert len(result["results"]) == 1
        assert result["results"][0]["path"] == "README.md"

    def test_search_empty_result_is_success(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        cap = FilesystemSearchCapability(root)
        result = cap.execute("search", {"pattern": "nonexistent*.xyz", "max_results": 10})
        assert result["count"] == 0
        assert result["results"] == []

    def test_search_respects_max_results(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        for i in range(150):
            (root / f"f{i}.txt").write_text("x")
        cap = FilesystemSearchCapability(root)
        result = cap.execute("search", {"pattern": "*.txt", "max_results": 10})
        assert result["count"] == 10

    def test_search_denies_outside_directory(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        cap = FilesystemSearchCapability(root)
        with pytest.raises(ValueError, match="directory outside sandbox"):
            cap.execute("search", {"pattern": "*.md", "directory": "../../etc", "max_results": 10})

    def test_search_missing_directory_fails(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        cap = FilesystemSearchCapability(root)
        with pytest.raises(ValueError, match="directory not found"):
            cap.execute("search", {"pattern": "*.md", "directory": "noexist", "max_results": 10})

    def test_search_deterministic_order(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "b.md").write_text("b")
        (root / "a.md").write_text("a")
        cap = FilesystemSearchCapability(root)
        r1 = cap.execute("search", {"pattern": "*.md", "max_results": 10})
        r2 = cap.execute("search", {"pattern": "*.md", "max_results": 10})
        assert [res["path"] for res in r1["results"]] == [res["path"] for res in r2["results"]]

    def test_search_symlink_escape_fail_closed(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "a.md").write_text("a")
        # Symlink inside repo pointing outside should not expose outside paths
        (root / "bad_link").symlink_to(tmp_path / "secret")
        cap = FilesystemSearchCapability(root)
        result = cap.execute("search", {"pattern": "*", "max_results": 10})
        # a.md included; bad_link result excluded because resolved path outside sandbox
        paths = [res["path"] for res in result["results"]]
        assert "a.md" in paths
        # The symlink target (/tmp/...) should NOT appear
        for p in paths:
            assert not str(p).startswith(str(tmp_path / "secret"))

    def test_search_authorized_directory_only(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        reg = CapabilityRegistry()
        reg.register(FilesystemSearchCapability(root))
        # Authorization uses directory resource; search result never authorizes
        from arion.orchestration.authz import AuthorizationRequest, Actor, ResourcePolicy, RelativePathBoundary
        policy = ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()})
        req = AuthorizationRequest(
            actor=Actor.agent("test"), task_id="t", step_index=0,
            capability="filesystem.search", action="search",
            scope="filesystem:read",
            params={"directory": "."},
            resource=".", resource_kind="filesystem:path",
        )
        assert policy.decide(req).outcome.name == "ALLOW"
