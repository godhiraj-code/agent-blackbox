from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_blackbox.repo_preflight import load_manifest, run_repo_preflight


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "content").mkdir()
    (repo / "content" / "one.md").write_text("# One\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return repo


def test_passes_changes_inside_scope_and_invariants(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    manifest = {
        "baseline": "HEAD",
        "allow_paths": ["src/**"],
        "max_changed_files": 1,
        "invariants": [
            {"name": "content corpus", "type": "glob_count", "glob": "content/*.md", "exact": 1},
            {"name": "no visible dates", "type": "text_forbidden", "glob": "src/*.py", "regex": "Published on"},
        ],
    }

    report = run_repo_preflight(repo, manifest)

    assert report["status"] == "pass"
    assert report["changed_paths"] == ["src/app.py"]


def test_blocks_out_of_scope_protected_and_change_budget(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "content" / "one.md").write_text("deleted corpus replacement\n", encoding="utf-8")
    (repo / "unexpected.txt").write_text("new\n", encoding="utf-8")
    manifest = {
        "baseline": "HEAD",
        "allow_paths": ["src/**"],
        "protected_paths": ["content/**"],
        "max_changed_files": 1,
    }

    report = run_repo_preflight(repo, manifest)
    codes = [item["code"] for item in report["findings"]]

    assert report["status"] == "blocked"
    assert "outside_intended_scope" in codes
    assert "protected_path_changed" in codes
    assert "change_budget_exceeded" in codes


def test_blocks_corpus_regression_and_visible_date_policy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "content" / "one.md").unlink()
    (repo / "src" / "app.py").write_text("Published on 2026-07-15\n", encoding="utf-8")
    manifest = {
        "baseline": "HEAD",
        "invariants": [
            {"name": "blog corpus", "type": "glob_count", "glob": "content/*.md", "min": 1},
            {"name": "hidden dates", "type": "text_forbidden", "glob": "src/*.py", "regex": "Published on"},
        ],
    }

    report = run_repo_preflight(repo, manifest)

    assert report["status"] == "blocked"
    assert len([item for item in report["findings"] if item["code"] == "invariant_failed"]) == 2


def test_manifest_requires_baseline(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"allow_paths": ["src/**"]}), encoding="utf-8")

    with pytest.raises(ValueError, match="baseline"):
        load_manifest(path)
