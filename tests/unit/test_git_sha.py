"""Story 4.4 — git-SHA resolver: reads .git metadata directly (no subprocess),
resolves a symbolic HEAD through refs/ or packed-refs, and degrades safely
when .git is absent or corrupted. An env override always wins."""

from __future__ import annotations

from pathlib import Path

from clav.common.git_sha import UNKNOWN_SHA, resolve_git_sha

SHA = "abc123def456abc123def456abc123def456abc"


def _init_git_dir(root: Path) -> Path:
    git_dir = root / ".git"
    git_dir.mkdir()
    return git_dir


def test_resolves_sha_via_symbolic_ref(tmp_path) -> None:
    git_dir = _init_git_dir(tmp_path)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    refs_dir = git_dir / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text(f"{SHA}\n")

    assert resolve_git_sha(tmp_path) == SHA


def test_resolves_detached_head_directly(tmp_path) -> None:
    git_dir = _init_git_dir(tmp_path)
    (git_dir / "HEAD").write_text(f"{SHA}\n")

    assert resolve_git_sha(tmp_path) == SHA


def test_falls_back_to_packed_refs(tmp_path) -> None:
    git_dir = _init_git_dir(tmp_path)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    # No refs/heads/main file -- only a packed-refs entry (post gc).
    (git_dir / "packed-refs").write_text(f"# pack-refs\n{SHA} refs/heads/main\n")

    assert resolve_git_sha(tmp_path) == SHA


def test_unknown_when_no_git_dir(tmp_path) -> None:
    assert resolve_git_sha(tmp_path) == UNKNOWN_SHA


def test_unknown_when_ref_and_packed_refs_both_missing(tmp_path) -> None:
    git_dir = _init_git_dir(tmp_path)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    assert resolve_git_sha(tmp_path) == UNKNOWN_SHA


def test_env_override_takes_precedence(tmp_path, monkeypatch) -> None:
    _init_git_dir(tmp_path)
    monkeypatch.setenv("CLAV_GIT_SHA", "env-override-sha")

    assert resolve_git_sha(tmp_path) == "env-override-sha"
