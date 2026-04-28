"""Diff helpers: git ref vs working tree.

Returns the set of changed Python files plus per-symbol modifications
inferred by re-scanning both sides and comparing content_hash by
(file_path, qualified_name, kind).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from scanner import FileScan, scan_file


@dataclass
class Modification:
    file_path: str
    qualified_name: str
    kind: str
    change_type: str  # 'added' | 'modified' | 'removed' | 'moved'
    old_hash: str | None
    new_hash: str | None


def _git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def has_any_commit(repo: Path) -> bool:
    code, _, _ = _git(["rev-parse", "--verify", "HEAD"], repo)
    return code == 0


def changed_files_diff(repo: Path, base_ref: str) -> list[str]:
    code, out, err = _git(["diff", "--name-only", f"{base_ref}...HEAD"], repo)
    if code != 0:
        return []
    return [f for f in out.splitlines() if f.endswith(".py")]


def changed_files_worktree(repo: Path) -> list[str]:
    files: set[str] = set()
    if has_any_commit(repo):
        code, out, _ = _git(["diff", "--name-only", "HEAD"], repo)
        if code == 0:
            files.update(f for f in out.splitlines() if f.endswith(".py"))
    code, out, _ = _git(["ls-files", "--others", "--exclude-standard"], repo)
    if code == 0:
        files.update(f for f in out.splitlines() if f.endswith(".py"))
    return sorted(files)


def file_at_ref(repo: Path, ref: str, rel_path: str) -> str | None:
    code, out, _ = _git(["show", f"{ref}:{rel_path}"], repo)
    return out if code == 0 else None


def _scan_text(text: str, rel_path: str) -> FileScan | None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / rel_path
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        return scan_file(tmp, Path(td))


def _index(scan: FileScan | None) -> dict[tuple[str, str], tuple[str, str]]:
    """Map (qualified_name, kind) -> (content_hash, file_path)."""
    if not scan:
        return {}
    return {(s.qualified_name, s.kind): (s.content_hash, s.file_path) for s in scan.symbols}


def diff_modifications(
    repo: Path,
    base_ref: str | None,
    files: list[str],
) -> list[Modification]:
    """Compare each file at base_ref vs HEAD/worktree and emit per-symbol modifications."""
    mods: list[Modification] = []
    for rel in files:
        head_path = repo / rel
        head_scan = scan_file(head_path, repo) if head_path.exists() else None

        if base_ref:
            old_text = file_at_ref(repo, base_ref, rel)
            old_scan = _scan_text(old_text, rel) if old_text is not None else None
        else:
            # Worktree mode: compare against HEAD if available
            if has_any_commit(repo):
                old_text = file_at_ref(repo, "HEAD", rel)
                old_scan = _scan_text(old_text, rel) if old_text is not None else None
            else:
                old_scan = None

        old = _index(old_scan)
        new = _index(head_scan)

        for key, (new_hash, fp) in new.items():
            qname, kind = key
            if key not in old:
                mods.append(Modification(fp, qname, kind, "added", None, new_hash))
            elif old[key][0] != new_hash:
                mods.append(Modification(fp, qname, kind, "modified", old[key][0], new_hash))

        for key, (old_hash, fp) in old.items():
            if key not in new:
                qname, kind = key
                mods.append(Modification(fp, qname, kind, "removed", old_hash, None))
    return mods


def head_ref(repo: Path) -> str | None:
    code, out, _ = _git(["rev-parse", "HEAD"], repo)
    return out.strip() if code == 0 else None
