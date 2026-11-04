"""Validate command-specific inputs and share CLI options and dispatch metadata."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from rail.configuration import RuntimeConfig


if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal


class CommandNamespace(argparse.Namespace):
    """Carry parser-owned dispatch and output metadata for the selected command.

    :attr operation: the command selected by the root parser.
    :attr log_format: the validated diagnostic output format.
    :attr json_output: whether the command returns machine-readable output.
    :attr handler: the callback registered by the selected command parser.
    :attr command_parser: the selected parser used for argument diagnostics.
    """

    operation: str
    log_format: Literal["console", "json"]
    json_output: bool
    handler: Callable[[CommandNamespace], dict[str, object] | None]
    command_parser: argparse.ArgumentParser


class CheckoutArguments(BaseModel):
    """Validate a checkout directory shared by commands with no further inputs.

    :attr path: the directory selected for repository discovery.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    path: Path


def validate_command_arguments[Arguments: BaseModel](
    *, namespace: CommandNamespace, model: type[Arguments]
) -> Arguments:
    """Build the selected command's model and report invalid input through argparse.

    :param namespace: the parsed input and its selected parser.
    :param model: the input model owned by this command.

    :return: the validated command inputs without parser dispatch metadata.
    """
    try:
        return model.model_validate(obj=namespace, from_attributes=True)
    except ValidationError as error:
        message = "; ".join(
            f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
            for detail in error.errors(include_url=False, include_input=False)
        )
        namespace.command_parser.error(message=message)


def add_path_option(*, parser: argparse.ArgumentParser) -> None:
    """Add repository discovery input with a visible current-directory choice.

    :param parser: the command parser to extend.
    """
    parser.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="directory inside a checkout (default: current directory)",
    )


def add_checkout_options(*, parser: argparse.ArgumentParser) -> None:
    """Add checkout selection and machine-readable output options.

    :param parser: the command parser to extend.
    """
    add_path_option(parser=parser)
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="optional JSON output (default: console text)",
    )


def add_check_options(*, parser: argparse.ArgumentParser) -> None:
    """Add checkout, output, and per-attempt check duration options.

    :param parser: the command parser to extend.
    """
    add_checkout_options(parser=parser)
    parser.add_argument(
        "--timeout",
        type=int,
        default=RuntimeConfig.CHECK_TIMEOUT_SECONDS,
        help=(
            "seconds per check attempt "
            f"(default: {RuntimeConfig.CHECK_TIMEOUT_SECONDS})"
        ),
    )
