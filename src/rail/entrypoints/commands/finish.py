"""Register finalization inputs and report the published feature pull request."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from rail.application.contracts import FinishInput, PullRequest
from rail.commands import finish
from rail.entrypoints.arguments import add_check_options, validate_command_arguments


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


class FinishArguments(FinishInput):
    """Validate publication text together with CLI file and check options.

    :attr path: the checkout directory selected for finalization.
    :attr timeout: the maximum seconds per check attempt.
    :attr body_file: the description file, or None to use the literal body.
    """

    path: Path
    timeout: Annotated[int, Field(gt=0)]
    body_file: Path | None


def register_finish_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register finalization text, checkout, and check options.

    :param commands: the root parser's command collection.
    """
    parser = commands.add_parser(
        "finish", help="save, rebase, check, squash and publish a feature PR"
    )
    parser.add_argument(
        "--title", required=True, help="PR title and final commit subject"
    )
    add_check_options(parser=parser)
    description = parser.add_mutually_exclusive_group()
    description.add_argument(
        "--body", default="", help="literal PR description (default: empty)"
    )
    description.add_argument(
        "--body-file", type=Path, help="UTF-8 description file, or '-' for stdin"
    )
    parser.set_defaults(handler=run_finish_command, command_parser=parser)


def run_finish_command(namespace: CommandNamespace) -> dict[str, object]:
    """Read the selected description, finalize the feature, and print its PR URL.

    :param namespace: the selected command's parsed arguments and output mode.

    :return: the confirmed publication result for optional JSON rendering.
    """
    arguments = validate_command_arguments(namespace=namespace, model=FinishArguments)
    body = arguments.body
    if arguments.body_file is not None:
        body = (
            sys.stdin.buffer.read().decode("utf-8-sig")
            if str(arguments.body_file) == "-"
            else arguments.body_file.read_text(encoding="utf-8-sig")
        )
    result = finish(
        path=arguments.path,
        title=arguments.title,
        body=body,
        timeout=arguments.timeout,
    )
    if not namespace.json_output:
        print(PullRequest.model_validate(obj=result["pr"]).url)
    return result
