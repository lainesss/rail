"""Run configured checks with console output and actual exit results."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from rail.adapters.processes import CommandError, run_process
from rail.application.contracts import Argument, NonblankText, OperationError


if TYPE_CHECKING:
    from rail.application.contracts import Check


logger = logging.getLogger(name="workflow.checks")


class CheckResult(BaseModel):
    """Validate the recorded outcome of one check attempt.

    :attr name: the configured check label.
    :attr args: the literal command arguments.
    :attr exit_code: the process exit status, or None if execution could not finish.
    :attr details: an execution diagnostic when one was recorded.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: NonblankText
    args: tuple[Argument, ...]
    exit_code: int | None
    details: str | None = None


class CheckRecorder(BaseModel):
    """Run configured checks and collect their exit results.

    :attr root: the task folder.
    :attr timeout: the maximum duration of each child command.
    :attr results: completed command attempts in execution order.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    root: Path
    timeout: int = Field(gt=0)
    results: list[CheckResult] = Field(default_factory=list)

    def run_check(self, command: Check) -> None:
        """Execute one configured check attempt and reject its real nonzero exit.

        :param command: the project-owned command and retry allowance.

        :raises OperationError: if the completed check exits unsuccessfully.
        """
        logger.info("Running check: %s", command.name)
        try:
            result = run_process(
                args=command.args,
                cwd=self.root,
                timeout=self.timeout,
                capture=False,
                forward_to_stderr=True,
                check_status=False,
                project_environment=True,
            )
        except CommandError as error:
            self.results.append(
                CheckResult(
                    name=command.name,
                    args=command.args,
                    exit_code=None,
                    details=str(error),
                )
            )
            raise
        self.results.append(
            CheckResult(
                name=command.name, args=command.args, exit_code=result.returncode
            )
        )
        if result.returncode:
            raise OperationError(
                f"Check {command.name!r} exited {result.returncode}: "
                f"{' '.join(command.args)}. See console output.",
                code="check_failed",
            )
