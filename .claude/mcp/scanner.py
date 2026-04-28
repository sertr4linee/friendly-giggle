"""AST-based Python symbol scanner.

Walks every .py file under a root, extracts functions/classes/methods,
hashes their source, and emits a flat list of symbol records plus
intra-project dependency edges (calls, imports, inherits).
"""
from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".claude"}


@dataclass
class Symbol:
    file_path: str
    qualified_name: str
    kind: str  # 'func' | 'class' | 'method'
    parent_qname: str | None
    signature: str
    content_hash: str
    line_start: int
    line_end: int
    cyclomatic: int
    loc: int
    is_public: bool
    has_docstring: bool


@dataclass
class Edge:
    from_qname: str
    from_file: str
    to_name: str  # may be a local qname or external dotted module
    kind: str  # 'call' | 'import' | 'inherit'
    external: bool


@dataclass
class FileScan:
    file_path: str
    file_hash: str
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _node_source(source: str, node: ast.AST) -> str:
    try:
        return ast.get_source_segment(source, node) or ""
    except Exception:
        return ""


def _signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = [a.arg for a in node.args.args]
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        return f"{prefix}{node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) if hasattr(ast, "unparse") else "" for b in node.bases]
        return f"class {node.name}({', '.join(bases)})"
    return ""


def _cyclomatic(node: ast.AST) -> int:
    count = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                              ast.ExceptHandler, ast.With, ast.AsyncWith,
                              ast.IfExp, ast.Assert)):
            count += 1
        elif isinstance(child, ast.BoolOp):
            count += max(0, len(child.values) - 1)
        elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            count += sum(1 for g in child.generators if g.ifs) or 1
    return count


def _has_docstring(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
        return False
    body = getattr(node, "body", None)
    if not body:
        return False
    first = body[0]
    return isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)


class _SymbolCollector(ast.NodeVisitor):
    def __init__(self, source: str, file_path: str):
        self.source = source
        self.file_path = file_path
        self.symbols: list[Symbol] = []
        self.edges: list[Edge] = []
        self._stack: list[str] = []  # qualified name stack
        self._class_stack: list[str] = []
        self._known_qnames: set[str] = set()

    def _qname(self, name: str) -> str:
        return ".".join(self._stack + [name]) if self._stack else name

    def _record(self, node, kind: str):
        qname = self._qname(node.name)
        src = _node_source(self.source, node) or node.name
        sym = Symbol(
            file_path=self.file_path,
            qualified_name=qname,
            kind=kind,
            parent_qname=".".join(self._stack) if self._stack else None,
            signature=_signature(node),
            content_hash=hashlib.sha256(src.encode("utf-8", errors="replace")).hexdigest(),
            line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
            cyclomatic=_cyclomatic(node) if kind != "class" else 1,
            loc=max(1, getattr(node, "end_lineno", node.lineno) - node.lineno + 1),
            is_public=not node.name.startswith("_"),
            has_docstring=_has_docstring(node),
        )
        self.symbols.append(sym)
        self._known_qnames.add(qname)
        return qname

    def visit_ClassDef(self, node: ast.ClassDef):
        qname = self._record(node, "class")
        for base in node.bases:
            try:
                base_name = ast.unparse(base) if hasattr(ast, "unparse") else ""
            except Exception:
                base_name = ""
            if base_name:
                self.edges.append(Edge(qname, self.file_path, base_name, "inherit", external=True))
        self._stack.append(node.name)
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        kind = "method" if self._class_stack else "func"
        qname = self._record(node, kind)
        self._stack.append(node.name)
        # Collect call edges from body
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target = self._resolve_call(child.func)
                if target:
                    self.edges.append(Edge(qname, self.file_path, target, "call", external=True))
        self.generic_visit(node)
        self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        # Reuse FunctionDef logic
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.edges.append(Edge(
                from_qname=self._qname("<module>") if self._stack else "<module>",
                from_file=self.file_path,
                to_name=alias.name,
                kind="import",
                external=True,
            ))

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        for alias in node.names:
            target = f"{mod}.{alias.name}" if mod else alias.name
            self.edges.append(Edge(
                from_qname=self._qname("<module>") if self._stack else "<module>",
                from_file=self.file_path,
                to_name=target,
                kind="import",
                external=True,
            ))

    def _resolve_call(self, func_node: ast.AST) -> str | None:
        try:
            return ast.unparse(func_node) if hasattr(ast, "unparse") else None
        except Exception:
            return None


def scan_file(path: Path, project_root: Path) -> FileScan | None:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    rel = str(path.relative_to(project_root)).replace(os.sep, "/")
    collector = _SymbolCollector(source, rel)
    collector.visit(tree)
    return FileScan(
        file_path=rel,
        file_hash=_file_hash(source),
        symbols=collector.symbols,
        edges=collector.edges,
    )


def iter_python_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(Path(dirpath) / fn)
    return out


def scan_project(project_root: Path) -> list[FileScan]:
    return [s for s in (scan_file(p, project_root) for p in iter_python_files(project_root)) if s is not None]
