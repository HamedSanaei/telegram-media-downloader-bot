from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
agent_context = importlib.import_module("scripts.agent_context")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _repository(tmp_path: Path) -> Path:
    _write(tmp_path / "src" / "sample" / "__init__.py", "")
    _write(
        tmp_path / "src" / "sample" / "models.py",
        """from dataclasses import dataclass

@dataclass
class Café:
    label: str

    def render(self, prefix: str = "é") -> str:
        return prefix + self.label

async def load(item: Café, /, *, limit: int = 2) -> Café:
    return item
""",
    )
    _write(
        tmp_path / "src" / "sample" / "service.py",
        """from sample.models import Café, load

class Runner:
    async def run(self, item: Café) -> Café:
        return await load(item)
""",
    )
    _write(
        tmp_path / "tests" / "test_models.py",
        """from sample.models import Café

def test_cafe() -> None:
    assert Café("x").render() == "éx"
""",
    )
    _write(
        tmp_path / "tests" / "test_service.py",
        """from sample.service import Runner

def test_runner_type() -> None:
    assert Runner is not None
""",
    )
    return tmp_path


def test_discovers_functions_classes_methods_signatures_and_line_ranges(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    records, issues = agent_context.discover_symbols(root, "src/sample/models.py")

    assert not issues
    by_name = {record.qualified_name: record for record in records}
    cafe = by_name["sample.models.Café"]
    render = by_name["sample.models.Café.render"]
    load = by_name["sample.models.load"]
    assert cafe.kind == "class"
    assert cafe.signature == "class Café"
    assert cafe.start_line == 3
    assert cafe.end_line >= render.end_line
    assert render.kind == "method"
    assert render.signature == "def render(self, prefix: str = 'é') -> str"
    assert load.kind == "function"
    assert load.signature == "async def load(item: Café, /, *, limit: int = 2) -> Café"


def test_imports_and_reverse_imports_are_resolved_stably(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    imports, import_issues = agent_context.imports_for_path(root, "src/sample/service.py")
    reverse, reverse_issues = agent_context.reverse_imports(root, "src/sample/models.py")

    assert not import_issues
    assert not reverse_issues
    assert [(item.module, item.imported_name) for item in imports] == [
        ("sample.models", "Café"),
        ("sample.models", "load"),
    ]
    assert [(item.path, item.imported_name) for item in reverse] == [
        ("src/sample/service.py", "Café"),
        ("src/sample/service.py", "load"),
        ("tests/test_models.py", "Café"),
    ]


def test_references_include_utf8_names_without_source_body_output(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    records, issues = agent_context.references(root, "Café")

    assert not issues
    assert [(item.path, item.expression) for item in records] == [
        ("src/sample/models.py", "Café"),
        ("src/sample/models.py", "Café"),
        ("src/sample/service.py", "Café"),
        ("src/sample/service.py", "Café"),
        ("tests/test_models.py", "Café"),
    ]
    rendered = [f"{item.path}:{item.line}:{item.column} {item.expression}" for item in records]
    assert all("return " not in line for line in rendered)


def test_likely_tests_rank_import_and_symbol_matches(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    by_path, issues = agent_context.likely_tests(root, "src/sample/service.py")
    by_symbol, symbol_issues = agent_context.likely_tests(root, "sample.models.Café")

    assert not issues
    assert not symbol_issues
    assert by_path[0].path == "tests/test_service.py"
    assert "imports target module" in by_path[0].reasons
    assert by_symbol[0].path == "tests/test_models.py"
    assert "references target symbol" in by_symbol[0].reasons


def test_results_are_stably_ordered_and_output_is_bounded(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    first, _ = agent_context.discover_symbols(root)
    second, _ = agent_context.discover_symbols(root)
    lines = [record.qualified_name for record in first]

    assert first == second
    assert lines == [record.qualified_name for record in second]
    assert agent_context.bounded(lines, 2) == [
        lines[0],
        lines[1],
        f"... {len(lines) - 2} more result(s); refine the target or raise --limit",
    ]


def test_malformed_python_is_reported_without_crashing(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "broken.py", "def broken(:\n")

    records, issues = agent_context.discover_symbols(tmp_path)

    assert records == []
    assert len(issues) == 1
    assert issues[0].path == "src/broken.py"
    assert issues[0].line == 1
    assert issues[0].message


def test_target_cannot_escape_repository(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    _write(outside, "value = 1\n")

    try:
        agent_context.python_files(tmp_path, outside)
    except ValueError as exc:
        assert "inside the repository" in str(exc)
    else:
        raise AssertionError("repository escape was not rejected")


def test_cli_symbol_output_is_bounded_and_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)

    result = agent_context.main(
        ["--root", str(root), "--limit", "1", "symbols", "src/sample/models.py"]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out.splitlines()[0].startswith("src/sample/models.py:3-")
    assert captured.out.splitlines()[-1].startswith("... ")
