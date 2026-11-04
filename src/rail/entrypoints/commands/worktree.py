"""Register worktree creation and print its completed checkout path."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rail.application.contracts import NonblankText
from rail.commands import create_worktree
from rail.entrypoints.arguments import (
    CheckoutArguments,
    add_checkout_options,
    validate_command_arguments,
)


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


class WorktreeArguments(CheckoutArguments):
    """Validate the source checkout, required destination, and new branch name.

    :attr destination: the directory to create for the task checkout.
    :attr branch: the new task branch name.
    """

    destination: Path
    branch: NonblankText


def register_worktree_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register required worktree creation inputs and output options.

    :param commands: the root parser's command collection.
    """
    group = commands.add_parser(
        "worktree", help="create an isolated checkout on a new branch"
    )
    selections = group.add_subparsers(required=True)
    parser = selections.add_parser(
        "create",
        help="create a new worktree and branch from the updated configured base",
    )
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--branch", required=True, help="new branch name")
    add_checkout_options(parser=parser)
    parser.set_defaults(handler=run_worktree_command, command_parser=parser)


def run_worktree_command(namespace: CommandNamespace) -> dict[str, object]:
    """Create a task checkout and print its path after successful creation.

    :param namespace: the selected command's parsed arguments and output mode.

    :return: the created checkout result for optional JSON rendering.
    """
    arguments = validate_command_arguments(namespace=namespace, model=WorktreeArguments)
    result = create_worktree(
        path=arguments.path, destination=arguments.destination, branch=arguments.branch
    )
    if not namespace.json_output:
        print(result["path"])
    return result
