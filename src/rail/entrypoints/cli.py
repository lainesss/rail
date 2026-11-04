"""Register CLI commands and handle shared logging, JSON output, and exit status."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from filelock import Timeout
from pydantic import ValidationError

from rail.application.contracts import OperationError, WorkflowError
from rail.entrypoints.arguments import CommandNamespace
from rail.entrypoints.commands.branch import register_branch_command
from rail.entrypoints.commands.check import register_check_command
from rail.entrypoints.commands.fetch import register_fetch_command
from rail.entrypoints.commands.finish import register_finish_command
from rail.entrypoints.commands.inspect import register_inspect_command
from rail.entrypoints.commands.install import register_install_command
from rail.entrypoints.commands.release import register_release_command
from rail.entrypoints.commands.worktree import register_worktree_command
from rail.entrypoints.logging import configure_logging


if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import NoReturn


logger = logging.getLogger(name="workflow")


class CommandParser(argparse.ArgumentParser):
    """Render argument errors using the application console prefix."""

    def error(self, message: str) -> NoReturn:
        """Write normal usage and a prefixed error, then exit with status two.

        :param message: the argument validation failure.
        """
        self.print_usage(file=sys.stderr)
        self.exit(status=2, message=f"[RAIL] ERROR: {message}\n")


def build_parser() -> CommandParser:
    """Build the command interface and register each command's own callback.

    :return: the configured root argument parser.
    """
    result = CommandParser(
        prog="rail", description="Manage feature branches and releases."
    )
    try:
        installed_version = version(distribution_name="rail")
    except PackageNotFoundError:
        installed_version = "0.0.0"
    result.add_argument(
        "--version", action="version", version=f"rail {installed_version}"
    )
    result.add_argument(
        "--log-format",
        choices=("console", "json"),
        default="console",
        help="diagnostic format (default: console)",
    )
    commands = result.add_subparsers(dest="operation", required=True)
    register_install_command(commands=commands)
    register_check_command(commands=commands)
    register_finish_command(commands=commands)
    register_release_command(commands=commands)
    register_inspect_command(commands=commands)
    register_fetch_command(commands=commands)
    register_branch_command(commands=commands)
    register_worktree_command(commands=commands)
    return result


def run_cli(*, argv: Sequence[str] | None = None) -> int:
    """Execute the selected command and translate errors at the process boundary.

    :param argv: the arguments to parse, or None for process arguments.

    :return: zero on success, one on operation failure, or 130 on interruption.
    """
    command_parser = build_parser()
    parsed = command_parser.parse_args(args=argv, namespace=CommandNamespace())
    configure_logging(format_name=parsed.log_format)
    try:
        result = parsed.handler(parsed)
        if parsed.json_output:
            print(
                json.dumps(
                    {"version": 1, "ok": True, "result": result}, ensure_ascii=True
                )
            )
        return 0
    except (WorkflowError, OSError, ValueError, KeyboardInterrupt, Timeout) as error:
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
        category = (
            error.code
            if isinstance(error, OperationError)
            else (
                "invalid_request"
                if isinstance(error, ValidationError)
                else (
                    "busy"
                    if isinstance(error, Timeout)
                    else "interrupted"
                    if code == 130
                    else "operation_failed"
                )
            )
        )
        message = str(error) or "Interrupted; completed effects are preserved"
        if parsed.json_output:
            print(
                json.dumps(
                    {
                        "version": 1,
                        "ok": False,
                        "error": {"code": category, "message": message},
                    },
                    ensure_ascii=True,
                )
            )
        else:
            logger.error("%s failed: %s", parsed.operation, message)
        return code
