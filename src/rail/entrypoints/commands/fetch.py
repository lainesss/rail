"""Register remote synchronization and report its completed checkout."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.commands import fetch
from rail.entrypoints.arguments import (
    CheckoutArguments,
    add_checkout_options,
    validate_command_arguments,
)


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


def register_fetch_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register remote fetching and its checkout and output options.

    :param commands: the root parser's command collection.
    """
    parser = commands.add_parser(
        "fetch", help="fetch all branches from all Git remotes"
    )
    add_checkout_options(parser=parser)
    parser.set_defaults(handler=run_fetch_command, command_parser=parser)


def run_fetch_command(namespace: CommandNamespace) -> dict[str, object]:
    """Fetch remote branches and report the checkout that was synchronized.

    :param namespace: the selected command's parsed arguments and output mode.

    :return: the synchronized checkout result for optional JSON rendering.
    """
    arguments = validate_command_arguments(namespace=namespace, model=CheckoutArguments)
    result = fetch(path=arguments.path)
    if not namespace.json_output:
        print(f"[RAIL] Fetch completed for {result['path']}")
    return result
