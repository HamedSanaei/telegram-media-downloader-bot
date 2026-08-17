#!/usr/bin/env python3
"""Deterministic, bounded repository navigation fallback for Python code.

Graphify is the preferred structural index. This utility stays dependency-free so a
developer or CI job can still locate symbols, imports, references, and likely tests.
It prints locations and signatures, never source bodies.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIMIT = 80
MAX_LIMIT = 500
SEARCH_ROOTS = ("src", "tests", "scripts", "plugins")
SKIP_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "venv",
}


@dataclass(frozen=True, slots=True)
class ParseIssue:
    path: str
    line: int
    message: str


@dataclass(frozen=True, slots=True)
class ParsedPython:
    path: Path
    relative_path: str
    module: str
    source: str
    tree: ast.Module


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    path: str
    module: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str


@dataclass(frozen=True, slots=True)
class ImportRecord:
    path: str
    line: int
    module: str
    imported_name: str | None
    display: str


@dataclass(frozen=True, slots=True)
class ReferenceRecord:
    path: str
    line: int
    column: int
    expression: str


@dataclass(frozen=True, slots=True)
class TestMatch:
    path: str
    score: int
    reasons: tuple[str, ...]


def _display_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _module_name(root: Path, path: Path) -> str:
    relative = path.resolve().relative_to(root.resolve())
    parts = list(relative.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRECTORIES for part in path.parts)


def python_files(root: Path, target: str | Path | None = None) -> list[Path]:
    """Return stable, repository-confined Python paths for a target or default roots."""

    root = root.resolve()
    if target is not None and str(target) not in {"", "."}:
        candidate = (
            (root / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        )
        if not candidate.is_relative_to(root):
            raise ValueError("target must remain inside the repository root")
        candidates = [candidate]
    else:
        candidates = [root / name for name in SEARCH_ROOTS if (root / name).exists()]
        if not candidates:
            candidates = [root]

    found: set[Path] = set()
    for candidate in candidates:
        if candidate.is_file():
            if candidate.suffix == ".py" and not _is_skipped(candidate.relative_to(root)):
                found.add(candidate)
            continue
        if not candidate.is_dir():
            raise FileNotFoundError(candidate)
        for path in candidate.rglob("*.py"):
            if not _is_skipped(path.relative_to(root)):
                found.add(path)
    return sorted(found, key=lambda path: _display_path(root, path))


def parse_python(root: Path, path: Path) -> tuple[ParsedPython | None, ParseIssue | None]:
    root = root.resolve()
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, ParseIssue(_display_path(root, path), 0, exc.__class__.__name__)
    try:
        tree = ast.parse(source, filename=_display_path(root, path))
    except SyntaxError as exc:
        return None, ParseIssue(
            _display_path(root, path),
            exc.lineno or 0,
            exc.msg,
        )
    return (
        ParsedPython(
            path=path,
            relative_path=_display_path(root, path),
            module=_module_name(root, path),
            source=source,
            tree=tree,
        ),
        None,
    )


def _annotation(node: ast.expr | None) -> str:
    return f": {ast.unparse(node)}" if node is not None else ""


def _default(node: ast.expr | None) -> str:
    return f" = {ast.unparse(node)}" if node is not None else ""


def _argument(argument: ast.arg, default: ast.expr | None = None) -> str:
    return f"{argument.arg}{_annotation(argument.annotation)}{_default(default)}"


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    default_offset = len(positional) - len(arguments.defaults)
    pieces: list[str] = []
    for index, argument in enumerate(positional):
        default = arguments.defaults[index - default_offset] if index >= default_offset else None
        pieces.append(_argument(argument, default))
        if arguments.posonlyargs and index + 1 == len(arguments.posonlyargs):
            pieces.append("/")

    if arguments.vararg is not None:
        pieces.append(f"*{_argument(arguments.vararg)}")
    elif arguments.kwonlyargs:
        pieces.append("*")

    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        pieces.append(_argument(argument, default))
    if arguments.kwarg is not None:
        pieces.append(f"**{_argument(arguments.kwarg)}")

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({', '.join(pieces)}){returns}"


def _class_signature(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    bases.extend(
        f"{keyword.arg}={ast.unparse(keyword.value)}"
        for keyword in node.keywords
        if keyword.arg is not None
    )
    suffix = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{suffix}"


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, parsed: ParsedPython) -> None:
        self.parsed = parsed
        self.stack: list[tuple[str, str]] = []
        self.records: list[SymbolRecord] = []

    def _add(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        kind: str,
        signature: str,
    ) -> None:
        names = [name for name, _ in self.stack]
        qualified_parts = [part for part in (self.parsed.module, *names, node.name) if part]
        decorator_lines = [decorator.lineno for decorator in node.decorator_list]
        self.records.append(
            SymbolRecord(
                path=self.parsed.relative_path,
                module=self.parsed.module,
                name=node.name,
                qualified_name=".".join(qualified_parts),
                kind=kind,
                start_line=min([node.lineno, *decorator_lines]),
                end_line=node.end_lineno or node.lineno,
                signature=signature,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node, kind="class", signature=_class_signature(node))
        self.stack.append((node.name, "class"))
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self.stack and self.stack[-1][1] == "class" else "function"
        self._add(node, kind=kind, signature=_function_signature(node))
        self.stack.append((node.name, kind))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def symbols_in_parsed(parsed: ParsedPython) -> list[SymbolRecord]:
    visitor = _SymbolVisitor(parsed)
    visitor.visit(parsed.tree)
    return sorted(
        visitor.records,
        key=lambda item: (item.path, item.start_line, item.qualified_name, item.kind),
    )


def discover_symbols(
    root: Path, target: str | Path | None = None
) -> tuple[list[SymbolRecord], list[ParseIssue]]:
    records: list[SymbolRecord] = []
    issues: list[ParseIssue] = []
    for path in python_files(root, target):
        parsed, issue = parse_python(root, path)
        if issue is not None:
            issues.append(issue)
        elif parsed is not None:
            records.extend(symbols_in_parsed(parsed))
    return (
        sorted(records, key=lambda item: (item.path, item.start_line, item.qualified_name)),
        sorted(issues, key=lambda item: (item.path, item.line, item.message)),
    )


def _resolve_from_module(parsed: ParsedPython, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package_parts = parsed.module.split(".") if parsed.module else []
    if parsed.path.name != "__init__.py" and package_parts:
        package_parts.pop()
    remove = max(0, node.level - 1)
    if remove:
        package_parts = package_parts[:-remove] if remove <= len(package_parts) else []
    if node.module:
        package_parts.extend(node.module.split("."))
    return ".".join(package_parts)


def imports_in_parsed(parsed: ParsedPython) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for node in ast.walk(parsed.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                display = f"import {alias.name}"
                if alias.asname:
                    display += f" as {alias.asname}"
                records.append(
                    ImportRecord(parsed.relative_path, node.lineno, alias.name, None, display)
                )
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_from_module(parsed, node)
            raw_module = f"{'.' * node.level}{node.module or ''}"
            for alias in node.names:
                display = f"from {raw_module} import {alias.name}"
                if alias.asname:
                    display += f" as {alias.asname}"
                records.append(
                    ImportRecord(parsed.relative_path, node.lineno, module, alias.name, display)
                )
    return sorted(records, key=lambda item: (item.path, item.line, item.display))


def imports_for_path(root: Path, target: str | Path) -> tuple[list[ImportRecord], list[ParseIssue]]:
    records: list[ImportRecord] = []
    issues: list[ParseIssue] = []
    for path in python_files(root, target):
        parsed, issue = parse_python(root, path)
        if issue is not None:
            issues.append(issue)
        elif parsed is not None:
            records.extend(imports_in_parsed(parsed))
    return records, issues


def reverse_imports(root: Path, target: str | Path) -> tuple[list[ImportRecord], list[ParseIssue]]:
    target_files = python_files(root, target)
    if len(target_files) != 1:
        raise ValueError("reverse-imports requires exactly one Python file")
    target_path = target_files[0]
    target_module = _module_name(root.resolve(), target_path)
    records: list[ImportRecord] = []
    issues: list[ParseIssue] = []
    for path in python_files(root):
        if path == target_path:
            continue
        parsed, issue = parse_python(root, path)
        if issue is not None:
            issues.append(issue)
            continue
        if parsed is None:
            continue
        for record in imports_in_parsed(parsed):
            imported_symbol = (
                f"{record.module}.{record.imported_name}"
                if record.module and record.imported_name
                else record.module
            )
            if (
                record.module == target_module
                or record.module.startswith(f"{target_module}.")
                or imported_symbol == target_module
            ):
                records.append(record)
    return (
        sorted(records, key=lambda item: (item.path, item.line, item.display)),
        sorted(issues, key=lambda item: (item.path, item.line, item.message)),
    )


def _attribute_name(node: ast.Attribute) -> str:
    parts = [node.attr]
    value: ast.expr = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def references(root: Path, symbol: str) -> tuple[list[ReferenceRecord], list[ParseIssue]]:
    needle = symbol.rsplit(".", 1)[-1]
    records: list[ReferenceRecord] = []
    issues: list[ParseIssue] = []
    for path in python_files(root):
        parsed, issue = parse_python(root, path)
        if issue is not None:
            issues.append(issue)
            continue
        if parsed is None:
            continue
        parents = {
            child: parent
            for parent in ast.walk(parsed.tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(parsed.tree):
            expression: str | None = None
            if isinstance(node, ast.Attribute):
                dotted = _attribute_name(node)
                if dotted == symbol or dotted.rsplit(".", 1)[-1] == needle:
                    expression = dotted
            elif isinstance(node, ast.Name) and node.id == needle:
                parent = parents.get(node)
                if isinstance(parent, ast.Attribute) and parent.value is node:
                    continue
                expression = node.id
            if expression is not None:
                assert isinstance(node, (ast.Attribute, ast.Name))
                records.append(
                    ReferenceRecord(
                        parsed.relative_path,
                        node.lineno,
                        node.col_offset + 1,
                        expression,
                    )
                )
    return (
        sorted(records, key=lambda item: (item.path, item.line, item.column, item.expression)),
        sorted(issues, key=lambda item: (item.path, item.line, item.message)),
    )


def _is_test_path(relative_path: str) -> bool:
    path = Path(relative_path)
    return path.name.startswith("test_") or "tests" in path.parts


def likely_tests(root: Path, path_or_symbol: str) -> tuple[list[TestMatch], list[ParseIssue]]:
    root = root.resolve()
    candidate = (root / path_or_symbol).resolve()
    target_module = ""
    target_names: set[str] = set()
    target_stem = ""
    if candidate.is_file() and candidate.is_relative_to(root):
        target_module = _module_name(root, candidate)
        target_stem = candidate.stem.removeprefix("test_")
        parsed, issue = parse_python(root, candidate)
        if issue is None and parsed is not None:
            target_names.update(
                record.name
                for record in symbols_in_parsed(parsed)
                if record.kind in {"class", "function"} and not record.name.startswith("_")
            )
    else:
        target_names.add(path_or_symbol.rsplit(".", 1)[-1])
        target_module = path_or_symbol.rsplit(".", 1)[0] if "." in path_or_symbol else ""
        target_stem = path_or_symbol.rsplit(".", 1)[-1].casefold()

    matches: list[TestMatch] = []
    issues: list[ParseIssue] = []
    for path in python_files(root):
        relative_path = _display_path(root, path)
        if not _is_test_path(relative_path):
            continue
        parsed, issue = parse_python(root, path)
        if issue is not None:
            issues.append(issue)
            continue
        if parsed is None:
            continue
        score = 0
        reasons: set[str] = set()
        for record in imports_in_parsed(parsed):
            if target_module and (
                record.module == target_module or record.module.startswith(f"{target_module}.")
            ):
                score += 8
                reasons.add("imports target module")
            if record.imported_name in target_names:
                score += 6
                reasons.add("imports target symbol")
        referenced_names = {node.id for node in ast.walk(parsed.tree) if isinstance(node, ast.Name)}
        overlap = target_names & referenced_names
        if overlap:
            score += 4 * len(overlap)
            reasons.add("references target symbol")
        if target_stem and target_stem in path.stem.casefold():
            score += 2
            reasons.add("matching test filename")
        if score:
            matches.append(TestMatch(relative_path, score, tuple(sorted(reasons))))
    return (
        sorted(matches, key=lambda item: (-item.score, item.path)),
        sorted(issues, key=lambda item: (item.path, item.line, item.message)),
    )


def bounded(lines: Sequence[str], limit: int) -> list[str]:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    selected = list(lines[:limit])
    remaining = len(lines) - len(selected)
    if remaining:
        selected.append(f"... {remaining} more result(s); refine the target or raise --limit")
    return selected


def _issue_lines(issues: Iterable[ParseIssue]) -> list[str]:
    return [f"parse-error {item.path}:{item.line} {item.message}" for item in issues]


def _print(lines: Sequence[str], limit: int) -> None:
    for line in bounded(lines, limit):
        print(line)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="maximum result rows")
    subparsers = parser.add_subparsers(dest="command", required=True)

    overview = subparsers.add_parser("overview", help="summarize Python files and symbols")
    overview.add_argument("target", nargs="?", default=".")
    symbols = subparsers.add_parser("symbols", help="list symbols in a path")
    symbols.add_argument("target")
    symbol = subparsers.add_parser("symbol", help="locate an exact or short symbol name")
    symbol.add_argument("name")
    imports = subparsers.add_parser("imports", help="list imports in a path")
    imports.add_argument("target")
    reverse = subparsers.add_parser("reverse-imports", help="list modules importing one file")
    reverse.add_argument("target")
    refs = subparsers.add_parser("refs", help="find best-effort AST references")
    refs.add_argument("name")
    tests = subparsers.add_parser("tests", help="rank likely related tests")
    tests.add_argument("target")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    root = args.root.resolve()
    issues: list[ParseIssue]
    lines: list[str]
    try:
        if args.command == "overview":
            files = python_files(root, args.target)
            symbol_records, issues = discover_symbols(root, args.target)
            counts = {
                kind: sum(item.kind == kind for item in symbol_records)
                for kind in ("class", "function", "method")
            }
            lines = [
                f"root {root}",
                f"python-files {len(files)}",
                f"symbols {len(symbol_records)} (classes={counts['class']}, functions={counts['function']}, methods={counts['method']})",
                f"parse-issues {len(issues)}",
            ]
            lines.extend(_issue_lines(issues))
        elif args.command in {"symbols", "symbol"}:
            symbol_records, issues = discover_symbols(
                root, args.target if args.command == "symbols" else None
            )
            if args.command == "symbol":
                name = args.name
                symbol_records = [
                    item
                    for item in symbol_records
                    if item.name == name
                    or item.qualified_name == name
                    or item.qualified_name.endswith(f".{name}")
                ]
            lines = [
                f"{item.path}:{item.start_line}-{item.end_line} {item.kind} {item.qualified_name} {item.signature}"
                for item in symbol_records
            ]
            lines.extend(_issue_lines(issues))
        elif args.command == "imports":
            import_records, issues = imports_for_path(root, args.target)
            lines = [f"{item.path}:{item.line} {item.display}" for item in import_records]
            lines.extend(_issue_lines(issues))
        elif args.command == "reverse-imports":
            import_records, issues = reverse_imports(root, args.target)
            lines = [f"{item.path}:{item.line} {item.display}" for item in import_records]
            lines.extend(_issue_lines(issues))
        elif args.command == "refs":
            reference_records, issues = references(root, args.name)
            lines = [
                f"{item.path}:{item.line}:{item.column} {item.expression}"
                for item in reference_records
            ]
            lines.extend(_issue_lines(issues))
        else:
            test_matches, issues = likely_tests(root, args.target)
            lines = [
                f"{item.path} score={item.score} reasons={'; '.join(item.reasons)}"
                for item in test_matches
            ]
            lines.extend(_issue_lines(issues))
        _print(lines or ["no results"], args.limit)
        return 1 if issues else 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"agent-context error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(stdout_reconfigure):
        stdout_reconfigure(encoding="utf-8")
    if callable(stderr_reconfigure):
        stderr_reconfigure(encoding="utf-8")
    raise SystemExit(main())
