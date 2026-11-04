"""Register release selections and report the merged PR and published tag."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rail.application.contracts import PullRequest, ReleaseOptions
from rail.commands import release
from rail.entrypoints.arguments import add_checkout_options, validate_command_arguments


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


class ReleaseArguments(ReleaseOptions):
    """Validate release selections together with the selected checkout.

    :attr path: the task or primary checkout directory.
    """

    path: Path


def register_release_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register PR selection, optional tagging, and cleanup flags.

    :param commands: the root parser's command collection.
    """
    parser = commands.add_parser(
        "release", help="merge a feature PR with optional tagging and cleanup"
    )
    add_checkout_options(parser=parser)
    parser.add_argument(
        "--pr",
        dest="number",
        type=int,
        help="PR number; otherwise identify from current branch",
    )
    parser.add_argument(
        "--tag",
        action="store_true",
        help="publish the configured version tag (default: false)",
    )
    parser.add_argument(
        "--delete-local-branch",
        action="store_true",
        help="delete the local PR branch after merging (default: false)",
    )
    parser.add_argument(
        "--delete-remote-branch",
        action="store_true",
        help="delete the remote PR branch after merging (default: false)",
    )
    parser.add_argument(
        "--delete-worktree",
        action="store_true",
        help="remove the PR's clean secondary worktree after merging (default: false)",
    )
    parser.set_defaults(handler=run_release_command, command_parser=parser)


def run_release_command(namespace: CommandNamespace) -> dict[str, object]:
    """Release the selected PR and print its URL and optional tag.

    :param namespace: the selected command's parsed arguments and output mode.

    :return: the merged release result for optional JSON rendering.
    """
    arguments = validate_command_arguments(namespace=namespace, model=ReleaseArguments)
    result = release(
        path=arguments.path,
        pr=arguments.number,
        tag=arguments.tag,
        delete_local_branch=arguments.delete_local_branch,
        delete_remote_branch=arguments.delete_remote_branch,
        delete_worktree=arguments.delete_worktree,
    )
    if not namespace.json_output:
        print(PullRequest.model_validate(obj=result["pr"]).url)
        if result["tag"]:
            print(result["tag"])
    return result
