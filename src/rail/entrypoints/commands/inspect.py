"""Register checkout inspection and display the observed workspace fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from rail.commands import inspect
from rail.entrypoints.arguments import (
    CheckoutArguments,
    add_checkout_options,
    validate_command_arguments,
)


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


def register_inspect_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register read-only checkout inspection and its output options.

    :param commands: the root parser's command collection.
    """
    parser = commands.add_parser(
        "inspect", help="find the containing checkout and print its local Git state"
    )
    add_checkout_options(parser=parser)
    parser.set_defaults(handler=run_inspect_command, command_parser=parser)


def run_inspect_command(namespace: CommandNamespace) -> dict[str, object]:
    """Inspect the checkout and print its fields or an explicit null workspace.

    :param namespace: the selected command's parsed arguments and output mode.

    :return: the observed workspace result for optional JSON rendering.
    """
    arguments = validate_command_arguments(namespace=namespace, model=CheckoutArguments)
    result = inspect(path=arguments.path)
    if not namespace.json_output:
        adapter: TypeAdapter[dict[str, object] | None] = TypeAdapter(
            dict[str, object] | None
        )
        workspace = adapter.validate_python(result["workspace"], strict=True)
        if workspace is None:
            print("workspace: null")
        else:
            for name, value in workspace.items():
                print(f"{name}: {value}")
    return result
