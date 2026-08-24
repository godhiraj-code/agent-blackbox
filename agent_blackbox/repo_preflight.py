from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path
from typing import Any


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("preflight manifest must contain a JSON object")
    baseline = data.get("baseline")
    if not isinstance(baseline, str) or not baseline.strip():
        raise ValueError("preflight manifest requires a non-empty baseline git ref")
    for key in ("allow_paths", "protected_paths", "invariants"):
        if key in data and not isinstance(data[key], list):
            raise ValueError(f"manifest field {key} must be a list")
    if "max_changed_files" in data and (not isinstance(data["max_changed_files"], int) or data["max_changed_files"] < 0):
        raise ValueError("max_changed_files must be a non-negative integer")
    return data


def run_repo_preflight(repo: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    repo = repo.resolve()
    _git(repo, "rev-parse", "--show-toplevel")
    baseline = manifest["baseline"]
    _git(repo, "rev-parse", "--verify", f"{baseline}^{{commit}}")

    changed = _changed_paths(repo, baseline)
    allow_paths = [str(item) for item in manifest.get("allow_paths", [])]
    protected_paths = [str(item) for item in manifest.get("protected_paths", [])]
    findings: list[dict[str, Any]] = []

    if allow_paths:
        for path in changed:
            if not _matches_any(path, allow_paths):
                findings.append(_finding("outside_intended_scope", path, f"Changed path is outside allow_paths: {path}"))

    for path in changed:
        if _matches_any(path, protected_paths):
            findings.append(_finding("protected_path_changed", path, f"Protected path changed: {path}"))

    max_changed = manifest.get("max_changed_files")
    if max_changed is not None and len(changed) > max_changed:
        findings.append(
            _finding(
                "change_budget_exceeded",
                None,
                f"Changed file count {len(changed)} exceeds maximum {max_changed}",
            )
        )

    invariant_results = []
    for index, invariant in enumerate(manifest.get("invariants", []), start=1):
        result = _evaluate_invariant(repo, invariant, index)
        invariant_results.append(result)
        if not result["passed"]:
            findings.append(_finding("invariant_failed", result.get("path"), result["message"], invariant=result.get("name")))

    return {
        "schema_version": "1.0",
        "status": "pass" if not findings else "blocked",
        "repo": repo.name,
        "baseline": baseline,
        "changed_file_count": len(changed),
        "changed_paths": changed,
        "findings": findings,
        "invariants": invariant_results,
    }


def _changed_paths(repo: Path, baseline: str) -> list[str]:
    output = _git(repo, "diff", "--name-only", "--relative", baseline, "--")
    paths = {line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()}
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
    paths.update(line.strip().replace("\\", "/") for line in untracked.splitlines() if line.strip())
    return sorted(paths)


def _evaluate_invariant(repo: Path, invariant: object, index: int) -> dict[str, Any]:
    if not isinstance(invariant, dict):
        raise ValueError(f"invariant {index} must be an object")
    kind = invariant.get("type")
    name = str(invariant.get("name") or f"invariant-{index}")
    pattern = invariant.get("glob")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"invariant {name} requires glob")
    files = sorted(path for path in repo.glob(pattern) if path.is_file() and ".git" not in path.parts)

    if kind == "glob_count":
        count = len(files)
        passed = True
        limits = []
        for key, operation in (("exact", lambda a, b: a == b), ("min", lambda a, b: a >= b), ("max", lambda a, b: a <= b)):
            if key in invariant:
                expected = invariant[key]
                if not isinstance(expected, int) or expected < 0:
                    raise ValueError(f"invariant {name} field {key} must be a non-negative integer")
                passed = passed and operation(count, expected)
                limits.append(f"{key}={expected}")
        if not limits:
            raise ValueError(f"glob_count invariant {name} requires exact, min, or max")
        return {
            "name": name,
            "type": kind,
            "passed": passed,
            "observed": count,
            "message": f"{name}: found {count} files for {pattern}; expected {', '.join(limits)}",
        }

    if kind in {"text_required", "text_forbidden"}:
        regex = invariant.get("regex")
        if not isinstance(regex, str) or not regex:
            raise ValueError(f"invariant {name} requires regex")
        compiled = re.compile(regex, re.MULTILINE)
        matching = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except UnicodeError:
                continue
            if compiled.search(text):
                matching.append(path.relative_to(repo).as_posix())
        passed = bool(matching) if kind == "text_required" else not matching
        expectation = "at least one match" if kind == "text_required" else "no matches"
        return {
            "name": name,
            "type": kind,
            "passed": passed,
            "observed": matching[:20],
            "message": f"{name}: expected {expectation} for /{regex}/ in {pattern}; matched {len(matching)} file(s)",
        }

    raise ValueError(f"unsupported invariant type for {name}: {kind}")


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _finding(code: str, path: str | None, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "severity": "high", "path": path, "message": message, **extra}


def _git(repo: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return process.stdout
