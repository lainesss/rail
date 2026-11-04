"""Register check inputs, execute configured checks, and report their outcome."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from rail.commands import check
from rail.entrypoints.arguments import (
    CheckoutArguments,
    add_check_options,
    validate_command_arguments,
)


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


class CheckArguments(CheckoutArguments):
    """Validate checkout selection and the duration allowed for each check.

    :attr timeout: the maximum seconds per check attempt.
    """

    timeout: Annotated[int, Field(gt=0)]


def register_check_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register check options and their execution callback.

    :param commands: the root parser's command collection.
    """
    parser = commands.add_parser("check", help="run the configured checks in order")
    add_check_options(parser=parser)
    parser.set_defaults(handler=run_check_command, command_parser=parser)


def run_check_command(namespace: CommandNamespace) -> dict[str, object]:
    """Validate check inputs, execute the operation, and print its console response.

    :param namespace: the selected command's parsed arguments and output mode.

    :return: the completed check result for optional JSON rendering.
    """
    arguments = validate_command_arguments(namespace=namespace, model=CheckArguments)
    result = check(path=arguments.path, timeout=arguments.timeout)
    if not namespace.json_output:
        print(
            "[RAIL] Configured checks passed"
            if result["checks"]
            else "[RAIL] No checks were run"
        )
    return result
