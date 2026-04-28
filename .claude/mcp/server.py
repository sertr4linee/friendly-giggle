"""MCP server (stdio) — code analysis state broker.

Exposes 12 tools the 8 subagents use to read/write SQLite state.
Per-agent allowlists are enforced at the agent level via .claude/agents/*.md
`tools:` frontmatter — this server does not authenticate callers.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Make sibling modules importable
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402

from scanner import scan_project, iter_python_files, scan_file  # noqa: E402
from differ import (  # noqa: E402
    diff_modifications,
    changed_files_diff,
    changed_files_worktree,
    has_any_commit,
    head_ref,
)

from mcp.server.fastmcp import FastMCP  # noqa: E402

# ---------------------------------------------------------------------------
# Paths & DB
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(os.environ.get("CODEANALYSIS_PROJECT_ROOT", HERE.parent.parent)).resolve()
DB_PATH = PROJECT_ROOT / ".claude" / "db" / "analysis.db"
SCHEMA_PATH = PROJECT_ROOT / ".claude" / "db" / "schema.sql"
RULES_PATH = HERE / "verdict_rules.yaml"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if fresh:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    return conn


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("codeanalysis")


@mcp.tool()
def start_run(mode: str, base_ref: str | None = None) -> dict:
    """Begin a new analysis run. mode in {'diff','worktree'}. Returns {run_id, head_ref}."""
    if mode not in ("diff", "worktree"):
        return {"error": f"invalid mode: {mode}"}
    conn = _connect()
    head = head_ref(PROJECT_ROOT)
    cur = conn.execute(
        "INSERT INTO runs (mode, base_ref, head_ref, status) VALUES (?, ?, ?, 'running')",
        (mode, base_ref, head),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return {"run_id": rid, "head_ref": head, "mode": mode, "base_ref": base_ref}


@mcp.tool()
def scan_symbols() -> dict:
    """Scan all Python files under the project root and upsert into `symbols` and `dependencies`.

    Idempotent: existing symbols with the same content_hash are left untouched.
    Returns counts of scanned files and total symbols indexed.
    """
    conn = _connect()
    scans = scan_project(PROJECT_ROOT)

    sym_count = 0
    edge_count = 0
    qname_to_id: dict[tuple[str, str, str], int] = {}

    for fs in scans:
        # Pass 1: upsert all symbols (parents resolved later)
        for s in fs.symbols:
            row = conn.execute(
                "SELECT id, content_hash FROM symbols WHERE file_path=? AND qualified_name=? AND kind=?",
                (s.file_path, s.qualified_name, s.kind),
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    """INSERT INTO symbols (file_path, qualified_name, kind, signature,
                                            content_hash, line_start, line_end,
                                            cyclomatic, loc, is_public, has_docstring)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (s.file_path, s.qualified_name, s.kind, s.signature,
                     s.content_hash, s.line_start, s.line_end,
                     s.cyclomatic, s.loc, int(s.is_public), int(s.has_docstring)),
                )
                sid = cur.lastrowid
            else:
                sid = row["id"]
                if row["content_hash"] != s.content_hash:
                    conn.execute(
                        """UPDATE symbols SET signature=?, content_hash=?, line_start=?,
                           line_end=?, cyclomatic=?, loc=?, is_public=?, has_docstring=?
                           WHERE id=?""",
                        (s.signature, s.content_hash, s.line_start, s.line_end,
                         s.cyclomatic, s.loc, int(s.is_public), int(s.has_docstring), sid),
                    )
            qname_to_id[(s.file_path, s.qualified_name, s.kind)] = sid
            sym_count += 1

        # Pass 2: parent linkage
        for s in fs.symbols:
            if not s.parent_qname:
                continue
            sid = qname_to_id[(s.file_path, s.qualified_name, s.kind)]
            for parent_kind in ("class", "func", "method"):
                pid = qname_to_id.get((s.file_path, s.parent_qname, parent_kind))
                if pid:
                    conn.execute("UPDATE symbols SET parent_id=? WHERE id=?", (pid, sid))
                    break

    # Pass 3: edges. Resolve to_name to internal symbol id when possible.
    name_index: dict[str, int] = {}
    for (fp, qname, kind), sid in qname_to_id.items():
        name_index.setdefault(qname, sid)
        # also index leaf name (after last dot) for unqualified call lookups
        leaf = qname.rsplit(".", 1)[-1]
        name_index.setdefault(leaf, sid)

    for fs in scans:
        for e in fs.edges:
            from_id = qname_to_id.get((fs.file_path, e.from_qname, "func")) \
                or qname_to_id.get((fs.file_path, e.from_qname, "method")) \
                or qname_to_id.get((fs.file_path, e.from_qname, "class"))
            if from_id is None:
                # module-level imports etc.
                continue
            to_id = name_index.get(e.to_name) or name_index.get(e.to_name.rsplit(".", 1)[-1])
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO dependencies
                       (from_symbol_id, to_symbol_id, to_external, kind)
                       VALUES (?, ?, ?, ?)""",
                    (from_id, to_id, None if to_id else e.to_name, e.kind),
                )
                edge_count += 1
            except sqlite3.IntegrityError:
                pass

    # Recompute fan_in/fan_out
    conn.execute("""
        UPDATE symbols SET
            fan_out = (SELECT COUNT(*) FROM dependencies WHERE from_symbol_id = symbols.id),
            fan_in  = (SELECT COUNT(*) FROM dependencies WHERE to_symbol_id   = symbols.id)
    """)

    # Build coverage table: a test symbol is a func/method whose file matches test patterns
    # and whose qualified name suggests it tests another known symbol by name.
    conn.execute("DELETE FROM coverage")
    test_syms = conn.execute(
        """SELECT id, file_path, qualified_name FROM symbols
           WHERE kind IN ('func','method')
             AND (file_path LIKE 'test%' OR file_path LIKE '%/test%' OR file_path LIKE 'tests/%' OR file_path LIKE '%/tests/%' OR qualified_name LIKE 'test_%' OR qualified_name LIKE '%.test_%')"""
    ).fetchall()
    all_syms = conn.execute(
        "SELECT id, qualified_name FROM symbols WHERE kind IN ('func','method')"
    ).fetchall()
    name_map: dict[str, list[int]] = {}
    for r in all_syms:
        leaf = r["qualified_name"].rsplit(".", 1)[-1]
        name_map.setdefault(leaf, []).append(r["id"])
    for t in test_syms:
        leaf = t["qualified_name"].rsplit(".", 1)[-1]
        # strip "test_" prefix
        target = leaf[5:] if leaf.startswith("test_") else leaf
        for sid in name_map.get(target, []):
            if sid != t["id"]:
                conn.execute(
                    "INSERT OR IGNORE INTO coverage (symbol_id, test_symbol_id) VALUES (?, ?)",
                    (sid, t["id"]),
                )

    conn.commit()
    conn.close()
    return {"files_scanned": len(scans), "symbols_indexed": sym_count, "edges": edge_count}


@mcp.tool()
def compute_diff(run_id: int, base_ref: str | None = None) -> dict:
    """Resolve modifications for a run and write to `modifications`.

    base_ref None ⇒ working-tree mode. Returns counts by change_type.
    """
    conn = _connect()
    if base_ref:
        files = changed_files_diff(PROJECT_ROOT, base_ref)
    else:
        files = changed_files_worktree(PROJECT_ROOT)

    if not files and not has_any_commit(PROJECT_ROOT):
        # Brand-new repo: treat every current symbol as 'added'
        files = [str(p.relative_to(PROJECT_ROOT)).replace(os.sep, "/")
                 for p in iter_python_files(PROJECT_ROOT)]
        mods = []
        for rel in files:
            scan = scan_file(PROJECT_ROOT / rel, PROJECT_ROOT)
            if not scan:
                continue
            for s in scan.symbols:
                mods.append((rel, s.qualified_name, s.kind, "added", None, s.content_hash))
    else:
        modifications = diff_modifications(PROJECT_ROOT, base_ref, files)
        mods = [(m.file_path, m.qualified_name, m.kind, m.change_type, m.old_hash, m.new_hash)
                for m in modifications]

    counts = {"added": 0, "modified": 0, "removed": 0, "moved": 0}
    for rel, qname, kind, ct, old_h, new_h in mods:
        sid_row = conn.execute(
            "SELECT id FROM symbols WHERE file_path=? AND qualified_name=? AND kind=?",
            (rel, qname, kind),
        ).fetchone()
        sid = sid_row["id"] if sid_row else None
        try:
            conn.execute(
                """INSERT OR REPLACE INTO modifications
                   (run_id, symbol_id, file_path, qualified_name, change_type, old_hash, new_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, sid, rel, qname, ct, old_h, new_h),
            )
            counts[ct] = counts.get(ct, 0) + 1
        except sqlite3.IntegrityError:
            pass

    conn.execute("UPDATE runs SET status='analyzed' WHERE id=?", (run_id,))
    conn.commit()
    conn.close()
    return {"run_id": run_id, "files_changed": len(files), "modifications": counts}


@mcp.tool()
def query_symbols(file_path: str | None = None, qualified_name: str | None = None,
                  kind: str | None = None, limit: int = 100) -> list[dict]:
    """Read-only lookup against the symbols table."""
    conn = _connect()
    sql = "SELECT * FROM symbols WHERE 1=1"
    args: list[Any] = []
    if file_path:
        sql += " AND file_path = ?"; args.append(file_path)
    if qualified_name:
        sql += " AND qualified_name = ?"; args.append(qualified_name)
    if kind:
        sql += " AND kind = ?"; args.append(kind)
    sql += " LIMIT ?"; args.append(limit)
    rows = _rows(conn.execute(sql, args))
    conn.close()
    return rows


@mcp.tool()
def query_dependencies(symbol_id: int, direction: str = "out") -> list[dict]:
    """List dependency edges for a symbol. direction in {'out','in'}."""
    conn = _connect()
    if direction == "in":
        rows = _rows(conn.execute(
            """SELECT d.*, s.qualified_name AS from_qname, s.file_path AS from_file
               FROM dependencies d JOIN symbols s ON s.id = d.from_symbol_id
               WHERE d.to_symbol_id = ?""", (symbol_id,)))
    else:
        rows = _rows(conn.execute(
            """SELECT d.*, s.qualified_name AS to_qname, s.file_path AS to_file
               FROM dependencies d LEFT JOIN symbols s ON s.id = d.to_symbol_id
               WHERE d.from_symbol_id = ?""", (symbol_id,)))
    conn.close()
    return rows


@mcp.tool()
def query_modifications(run_id: int, change_type: str | None = None) -> list[dict]:
    """List symbol-level modifications recorded for a run."""
    conn = _connect()
    if change_type:
        rows = _rows(conn.execute(
            "SELECT * FROM modifications WHERE run_id=? AND change_type=?",
            (run_id, change_type)))
    else:
        rows = _rows(conn.execute(
            "SELECT * FROM modifications WHERE run_id=?", (run_id,)))
    conn.close()
    return rows


@mcp.tool()
def query_coverage(symbol_id: int) -> list[dict]:
    """Return test symbols that cover a given symbol (heuristic: name match)."""
    conn = _connect()
    rows = _rows(conn.execute(
        """SELECT c.test_symbol_id, s.file_path, s.qualified_name
           FROM coverage c JOIN symbols s ON s.id = c.test_symbol_id
           WHERE c.symbol_id = ?""", (symbol_id,)))
    conn.close()
    return rows


@mcp.tool()
def query_blame(symbol_id: int) -> dict:
    """Return git blame metadata for a symbol if recorded; otherwise empty dict."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM git_blame WHERE symbol_id=?", (symbol_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


@mcp.tool()
def record_finding(run_id: int, agent: str, severity: str, category: str,
                   message: str, symbol_id: int | None = None,
                   evidence: str | None = None) -> dict:
    """Insert a finding for a run."""
    if severity not in ("info", "warn", "error", "critical"):
        return {"error": f"invalid severity: {severity}"}
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO findings (run_id, agent, symbol_id, severity, category, message, evidence)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, agent, symbol_id, severity, category, message, evidence),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return {"finding_id": fid}


@mcp.tool()
def query_findings(run_id: int, agent: str | None = None,
                   severity: str | None = None) -> list[dict]:
    """List findings for a run, optionally filtered by agent or severity."""
    conn = _connect()
    sql = "SELECT * FROM findings WHERE run_id=?"
    args: list[Any] = [run_id]
    if agent:
        sql += " AND agent=?"; args.append(agent)
    if severity:
        sql += " AND severity=?"; args.append(severity)
    sql += " ORDER BY severity DESC, id ASC"
    rows = _rows(conn.execute(sql, args))
    conn.close()
    return rows


@mcp.tool()
def aggregate_verdict(run_id: int) -> dict:
    """Apply verdict_rules.yaml to all findings of a run; write decision deterministically."""
    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    weights = rules.get("score_weights", {})
    thresholds = rules.get("thresholds", [])
    hard_rules = rules.get("hard_rules", [])

    conn = _connect()
    findings = _rows(conn.execute(
        "SELECT severity, category, agent FROM findings WHERE run_id=?", (run_id,)
    ))

    # Hard rules first
    decision: str | None = None
    for hr in hard_rules:
        for f in findings:
            sev_match = ("if_severity" not in hr) or f["severity"] == hr["if_severity"]
            cat_match = ("if_category" not in hr) or f["category"] == hr["if_category"] \
                or f["category"].startswith(hr.get("if_category", "") + ".")
            if sev_match and cat_match:
                decision = hr["decision"]
                break
        if decision:
            break

    score = sum(weights.get(f["severity"], 0) for f in findings)

    if decision is None:
        for t in thresholds:
            if score <= t.get("when_score_at_or_below", 0):
                decision = t["decision"]
                break
        if decision is None:
            decision = "approve"

    by_sev: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        by_agent[f["agent"]] = by_agent.get(f["agent"], 0) + 1

    summary = json.dumps({"by_severity": by_sev, "by_agent": by_agent}, sort_keys=True)

    conn.execute(
        """INSERT OR REPLACE INTO verdicts (run_id, decision, score, summary)
           VALUES (?, ?, ?, ?)""",
        (run_id, decision, score, summary),
    )
    conn.commit()
    conn.close()
    return {"run_id": run_id, "decision": decision, "score": score,
            "by_severity": by_sev, "by_agent": by_agent, "total_findings": len(findings)}


@mcp.tool()
def finish_run(run_id: int, status: str = "finished") -> dict:
    """Mark a run as finished (or failed)."""
    if status not in ("finished", "failed"):
        return {"error": f"invalid status: {status}"}
    conn = _connect()
    conn.execute(
        "UPDATE runs SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, run_id),
    )
    conn.commit()
    conn.close()
    return {"run_id": run_id, "status": status}


if __name__ == "__main__":
    mcp.run()
