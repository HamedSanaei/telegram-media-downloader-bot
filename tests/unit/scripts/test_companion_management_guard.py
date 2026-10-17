import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("state,expected", [("stopped", 0), ("running", 1), ("error", 1)])
@pytest.mark.parametrize("backend", ["bash", "powershell"])
def test_management_refuses_a_running_or_unknown_companion_writer(
    tmp_path: Path,
    state: str,
    expected: int,
    backend: str,
) -> None:
    (tmp_path / "companion.local.yaml").write_text("web_companion: {}\n", encoding="utf-8")
    source = Path("scripts/lib/common.sh" if backend == "bash" else "scripts/tmb.ps1").resolve()
    if backend == "bash":
        executable = shutil.which("bash")
        if os.name == "nt":
            git_bash = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/bash.exe"
            executable = str(git_bash) if git_bash.is_file() else None
        if executable is None:
            pytest.skip("Bash is unavailable on this host")
        program = r"""
ROOT_DIR="$TEST_WRITER_ROOT"
source "$TEST_WRITER_SOURCE"
docker() {
  if [[ "$3" != 'label=com.docker.compose.service=companion' ]]; then return 9; fi
  case "$TEST_WRITER_STATE" in
    running) printf '%s\n' fixture-container ;;
    error) return 1 ;;
  esac
}
TMB_NO_LOCK=1
acquire_management_lock
"""
        command = [executable, "-c", program]
        input_script = None
    else:
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            pytest.skip("PowerShell is unavailable on this host")
        input_script = r"""
$ErrorActionPreference = 'Stop'
$RootDirectory = $env:TEST_WRITER_ROOT
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_WRITER_SOURCE,[ref]$tokens,[ref]$errors)
if ($errors.Count -gt 0) { throw 'PowerShell parse failure' }
$definition = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-CompanionStopped'}, $true)
Invoke-Expression $definition.Extent.Text
function Invoke-Docker {
  if ($env:TEST_WRITER_STATE -eq 'error') { throw 'fixture docker failure' }
  if ($env:TEST_WRITER_STATE -eq 'running') { Write-Output 'fixture-container' }
}
try { Assert-CompanionStopped; exit 0 } catch { Write-Output 'management refused'; exit 1 }
"""
        command = [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(input_script.encode("utf-16-le")).decode("ascii"),
        ]
        input_script = None
    environment = {
        **os.environ,
        "TEST_WRITER_STATE": state,
        "TEST_WRITER_ROOT": tmp_path.as_posix(),
        "TEST_WRITER_SOURCE": source.as_posix(),
    }
    result = subprocess.run(
        command,
        input=input_script,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if state == "running" and backend == "bash":
        assert "Stop the Instagram companion" in result.stderr
