"""Execute configured checks in order with individual retry allowances."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import WorkflowError


if TYPE_CHECKING:
    from collections.abc import Callable

    from rail.application.contracts import Check, CheckSettings


def run_checks(*, checks: CheckSettings, execute: Callable[[Check], None]) -> None:
    """Execute named checks in order using each check's retry allowance.

    :param checks: the ordered checks and their individual retry allowances.
    :param execute: the command execution boundary.
    """
    for command in checks.commands:
        remaining = command.retries
        while True:
            try:
                execute(command)
            except WorkflowError:
                if remaining == 0:
                    raise
                remaining -= 1
            else:
                break
