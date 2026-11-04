"""Exercise checks behavior with isolated repository resources."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from rail.adapters.checks import CheckRecorder
from rail.adapters.processes import CommandError
from rail.application.contracts import Check, OperationError


if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(argnames="exit_code", argvalues=[0, 9])
def test_check_results_and_console_output(
    *, tmp_path: Path, capfd: pytest.CaptureFixture[str], exit_code: int
) -> None:
    """Context: checks encounter a missing executable and a completed real child.

    Invariants:
        - Startup failure is distinct from a nonzero process exit.
        - Full check output reaches the console without being stored in results.
        - Attempts retain names, literal arguments, and exit statuses in order.
    """
    recorder = CheckRecorder(root=tmp_path, timeout=30)
    with pytest.raises(expected_exception=CommandError, match="not found"):
        recorder.run_check(
            Check(name="Missing", args=("rail-test-nonexistent",), retries=0)
        )
    command = Check(
        name="Large",
        args=(
            sys.executable,
            "-c",
            f"import sys; print('x'*9000); sys.exit({exit_code})",
        ),
        retries=0,
    )
    if exit_code:
        with pytest.raises(
            expected_exception=OperationError, match="exited 9"
        ) as failure:
            recorder.run_check(command)
        assert failure.value.code == "check_failed"
    else:
        recorder.run_check(command)
    assert capfd.readouterr().err.strip() == "x" * 9000
    assert [
        result.model_dump(mode="json", exclude_unset=True)
        for result in recorder.results
    ] == [
        {
            "name": "Missing",
            "args": ["rail-test-nonexistent"],
            "exit_code": None,
            "details": "Executable not found: rail-test-nonexistent",
        },
        {"name": "Large", "args": list(command.args), "exit_code": exit_code},
    ]
