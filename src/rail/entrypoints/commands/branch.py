"""Register branch creation and report the new branch name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import NonblankText
from rail.commands import create_branch
from rail.entrypoints.arguments import (
    CheckoutArguments,
    add_checkout_options,
    validate_command_arguments,
)


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


class BranchArguments(CheckoutArguments):
    """Validate the source checkout and requested branch name.

    :attr name: the new task branch name.
    """

    name: NonblankText


def register_branch_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register branch creation and its checkout and output options.

    :param commands: the root parser's command collection.
    """
    group = commands.add_parser("branch", help="manage local branches")
    selections = group.add_subparsers(required=True)
    parser = selections.add_parser(
        "create",
        help="create a branch from the updated configured base without switching",
    )
    parser.add_argument("--name", required=True, help="new branch name")
    add_checkout_options(parser=parser)
    parser.set_defaults(handler=run_branch_command, command_parser=parser)


def run_branch_command(namespace: CommandNamespace) -> dict[str, object]:
    """Create the requested branch and print its name after successful creation.

    :param namespace: the selected command's parsed arguments and output mode.

    :return: the created branch result for optional JSON rendering.
    """
    arguments = validate_command_arguments(namespace=namespace, model=BranchArguments)
    result = create_branch(path=arguments.path, name=arguments.name)
    if not namespace.json_output:
        print(f"[RAIL] Branch created: {result['branch']}")
    return result
