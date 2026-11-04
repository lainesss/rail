"""Check real subprocess behavior and controlled invocation failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from typing import TYPE_CHECKING
from unittest.mock import Mock

import psutil
import pytest

from rail.adapters.processes import CommandError, run_process, stop_process_tree
from rail.application.contracts import WorkflowError
from rail.configuration import RuntimeConfig


if TYPE_CHECKING:
    from pathlib import Path


def test_capture_and_environment(*, tmp_path: Path) -> None:
    """Context: a real child emits both streams and inspects execution context.

    Invariants:
        - Literal arguments and the explicit working directory reach the child.
        - Prompts are disabled and stdout/stderr remain distinguishable.
    """
    result = run_process(
        args=[
            sys.executable,
            "-c",
            "import os,sys; print(os.getcwd()); print(sys.argv[1]); "
            "print(os.environ['GH_PROMPT_DISABLED'], file=sys.stderr)",
            "literal;$(no-shell)",
        ],
        cwd=tmp_path,
    )
    assert str(tmp_path) in result.stdout
    assert "literal;$(no-shell)" in result.stdout
    assert result.stderr.splitlines() == ["1"]
    assert result.returncode == 0


def test_forward_output(*, tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """Context: a configured check forwards its streams directly.

    Invariants:
        - Subprocess output receives no application prefix.
        - Captured result text is empty when forwarding is selected.
    """
    result = run_process(
        args=[sys.executable, "-c", "print('raw')"],
        cwd=tmp_path,
        capture=False,
        timeout=None,
    )
    assert capfd.readouterr().out.splitlines() == ["raw"]
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(argnames="capture", argvalues=[True, False])
def test_nonzero_status(*, tmp_path: Path, capture: bool) -> None:
    """Context: a child command returns a nonzero exit status.

    Invariants:
        - The error preserves command identity and status.
        - Captured stderr or a forwarded-output explanation is available.
    """
    with pytest.raises(expected_exception=WorkflowError) as failure:
        run_process(
            args=[
                sys.executable,
                "-c",
                "import sys; print('cause',file=sys.stderr); sys.exit(9)",
            ],
            cwd=tmp_path,
            capture=capture,
        )
    assert "(9)" in str(failure.value)
    assert (
        "cause" in str(failure.value) if capture else "forwarded" in str(failure.value)
    )
    assert sys.executable in str(failure.value)


def test_allowed_status_and_timeout(*, tmp_path: Path) -> None:
    """Context: a status query allows one and a separate child exceeds its deadline.

    Invariants:
        - An allowed nonzero status is returned to its caller.
        - Timeout is reported distinctly from command failure.
    """
    assert (
        run_process(
            args=[sys.executable, "-c", "raise SystemExit(1)"],
            cwd=tmp_path,
            allowed=(0, 1),
        ).returncode
        == 1
    )
    with pytest.raises(expected_exception=WorkflowError, match="timed out"):
        run_process(
            args=[sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            timeout=0.02,
        )


@pytest.mark.parametrize(argnames="stream", argvalues=["stdout", "stderr"])
@pytest.mark.parametrize(argnames="character", argvalues=["x", "é"])
@pytest.mark.parametrize(
    argnames="excess", argvalues=[0, 1], ids=["at-limit", "over-limit"]
)
def test_query_size_limit(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
    character: str,
    excess: int,
) -> None:
    """Context: a real child's ASCII or UTF-8 stream reaches its byte limit.

    Invariants:
        - Output exactly at the limit is returned intact in the correct stream.
        - One excess byte rejects the query instead of returning truncated output.
    """
    monkeypatch.setattr(target=RuntimeConfig, name="CAPTURE_LIMIT_BYTES", value=64)
    payload = character * (64 // len(character.encode(encoding="utf-8"))) + "x" * excess
    arguments = [
        sys.executable,
        "-c",
        f"import sys; sys.{stream}.buffer.write({payload!r}.encode('utf-8'))",
    ]
    if excess:
        with pytest.raises(expected_exception=CommandError, match="query rejected"):
            run_process(args=arguments, cwd=tmp_path)
    else:
        result = run_process(args=arguments, cwd=tmp_path)
        assert result.stdout == (payload if stream == "stdout" else "")
        assert result.stderr == (payload if stream == "stderr" else "")
        assert result.returncode == 0


@pytest.mark.parametrize(argnames="path", argvalues=[None, "unsafe.cmd", "unsafe.bat"])
def test_missing_and_shell_launchers(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str | None
) -> None:
    """Context: executable discovery fails or resolves a Windows shell launcher.

    Invariants:
        - The invocation fails before spawning a process.
    """
    monkeypatch.setattr(
        target="rail.adapters.processes.shutil.which", name=Mock(return_value=path)
    )
    execute = Mock()
    monkeypatch.setattr(target="rail.adapters.processes.subprocess.Popen", name=execute)
    with pytest.raises(expected_exception=WorkflowError):
        run_process(args=["tool"], cwd=tmp_path)
    execute.assert_not_called()


def test_startup_failure(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Context: a discovered executable fails to start.

    Invariants:
        - The error identifies startup failure and preserves its cause.
    """
    monkeypatch.setattr(
        target="rail.adapters.processes.subprocess.Popen",
        name=Mock(side_effect=PermissionError(13, "denied")),
    )
    with pytest.raises(
        expected_exception=WorkflowError, match="Unable to start"
    ) as failure:
        run_process(args=[sys.executable], cwd=tmp_path)
    assert isinstance(failure.value.__cause__, PermissionError)


def test_checkout_environment(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a configured check needs the checkout-local Python interpreter.

    Invariants:
        - Project tools resolve inside the checkout environment.
        - Inherited Git directory overrides never reach project checks.
    """
    venv.EnvBuilder(with_pip=False).create(env_dir=tmp_path / ".venv")
    overrides = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")
    for name in overrides:
        monkeypatch.setenv(name=name, value="foreign")
    result = run_process(
        args=[
            "python",
            "-c",
            "import os,sys,json; "
            f"keys={overrides!r}; "
            "print(json.dumps([sys.prefix, [os.getenv(key) for key in keys]]))",
        ],
        cwd=tmp_path,
        project_environment=True,
    )
    prefix, inherited = json.loads(result.stdout)
    assert prefix == str(tmp_path / ".venv")
    assert inherited == [None] * len(overrides)
    assert all(os.environ[name] == "foreign" for name in overrides)


def test_timeout_stops_observed_descendants(*, tmp_path: Path) -> None:
    """Context: a timed-out program starts a long-running native descendant.

    Invariants:
        - The owned descendant is stopped before the timeout is returned.
    """
    marker = tmp_path / "child.pid"
    program = (
        "import subprocess,sys,time,pathlib; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
    )
    with pytest.raises(CommandError, match="timed out"):
        run_process(
            args=[sys.executable, "-c", program, str(marker)], cwd=tmp_path, timeout=2
        )
    assert marker.exists()
    assert not psutil.pid_exists(pid=int(marker.read_text()))


@pytest.mark.parametrize("denied", [True, False])
def test_descendant_cleanup_failure_still_reaps_parent(
    *, monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    """Context: the OS denies descendant termination or reports a surviving child.

    Invariants:
        - Cleanup reports the failure and still kills and reaps its owned parent.
    """
    child = Mock(spec=subprocess.Popen, pid=os.getpid())
    descendant = Mock(spec=psutil.Process)
    if denied:
        descendant.kill.side_effect = psutil.AccessDenied(pid=1)
    parent = Mock(spec=psutil.Process)
    parent.children.return_value = [descendant]
    monkeypatch.setattr(
        target="rail.adapters.processes.psutil.Process", name=Mock(return_value=parent)
    )
    monkeypatch.setattr(
        target="rail.adapters.processes.psutil.wait_procs",
        name=Mock(return_value=([], [descendant])),
    )
    with pytest.raises(CommandError, match="descendants"):
        stop_process_tree(child=child)
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=10)
