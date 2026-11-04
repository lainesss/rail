"""Expose repository commands as ordinary Python calls shared with the CLI."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rail.adapters.checks import CheckRecorder
from rail.adapters.configuration import install_configuration, read_settings
from rail.adapters.git.repository import Git, discover_git_root
from rail.adapters.github import GitHub
from rail.adapters.locking import create_operation_lock
from rail.application.contracts import FinishInput, OperationError, ReleaseOptions
from rail.application.workflow.checks import run_checks
from rail.application.workflow.finish import publish_feature
from rail.application.workflow.release import release_pull_request
from rail.application.workspaces import (
    create_branch as make_branch,
    create_worktree as make_worktree,
    inspect_workspace,
)
from rail.configuration import RuntimeConfig


if TYPE_CHECKING:
    from rail.application.contracts import InstallationSettings


def _open_repository(*, path: Path | None) -> Git:
    """Resolve the requested directory to an ordinary or linked checkout.

    :param path: a directory inside the checkout, or None for the current directory.

    :return: the checkout adapter with implicit hooks disabled.

    :raises OperationError: if the requested path has no containing Git repository.
    """
    root = discover_git_root(
        path=(path if path is not None else Path.cwd()).expanduser().resolve()
    )
    if root is None:
        raise OperationError(
            "No Git repository contains the requested path.", code="not_repository"
        )
    return Git(root=root)


def install(
    *, settings: InstallationSettings, path: Path | None = None
) -> tuple[bool, str]:
    """Install configuration without interactive prompts.

    :param settings: the complete validated repository and release choices.
    :param path: a checkout directory, or None to use the current directory.

    :return: whether configuration was created and the saved line changes.
    """
    repository = _open_repository(path=path)
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        return install_configuration(root=repository.root, settings=settings)


def inspect(*, path: Path | None = None) -> dict[str, object]:
    """Read local checkout metadata using the saved project settings.

    :param path: a directory to inspect, or None to use the current directory.

    :return: workspace metadata, or a null workspace outside Git.
    """
    root = discover_git_root(
        path=(path if path is not None else Path.cwd()).expanduser().resolve()
    )
    if root is None:
        return {"workspace": None}
    return {
        "workspace": inspect_workspace(
            repository=Git(root=root),
            settings=read_settings(root=root),
        )
    }


def fetch(*, path: Path | None = None) -> dict[str, object]:
    """Fetch every Git remote's branches without moving local branches.

    :param path: a checkout directory, or None to use the current directory.

    :return: the absolute checkout path.
    """
    repository = _open_repository(path=path)
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        repository.remotes.fetch_remotes()
    return {"path": str(repository.root)}


def create_branch(*, name: str, path: Path | None = None) -> dict[str, object]:
    """Create a branch at the freshly fetched configured base from a clean checkout.

    :param name: the new branch name.
    :param path: a source checkout directory, or None to use the current directory.

    :return: the source checkout path and new branch name.
    """
    repository = _open_repository(path=path)
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        make_branch(
            repository=repository,
            settings=read_settings(root=repository.root),
            name=name,
        )
    return {"path": str(repository.root), "branch": name}


def create_worktree(
    *, destination: Path, branch: str, path: Path | None = None
) -> dict[str, object]:
    """Create a new branch and secondary checkout from the fetched configured base.

    :param destination: the new checkout directory.
    :param branch: the new feature branch name.
    :param path: a source checkout directory, or None to use the current directory.

    :return: the new checkout path and branch name.
    """
    repository = _open_repository(path=path)
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        created = make_worktree(
            repository=repository,
            settings=read_settings(root=repository.root),
            path=destination.expanduser().resolve(),
            branch=branch,
        )
    return {"path": str(created.root), "branch": branch}


def check(
    *,
    path: Path | None = None,
    timeout: int = RuntimeConfig.CHECK_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Run the configured checks and return their attempts and final workspace.

    :param path: a checkout directory, or None to use the current directory.
    :param timeout: the maximum seconds per check attempt.

    :return: workspace metadata and completed check attempts.
    """
    repository = _open_repository(path=path)
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        settings = read_settings(root=repository.root)
        recorder = CheckRecorder(root=repository.root, timeout=timeout)
        run_checks(checks=settings.checks, execute=recorder.run_check)
        return {
            "workspace": inspect_workspace(repository=repository, settings=settings),
            "checks": [
                result.model_dump(mode="json", exclude_unset=True)
                for result in recorder.results
            ],
        }


def finish(
    *,
    title: str,
    path: Path | None = None,
    body: str = "",
    timeout: int = RuntimeConfig.CHECK_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Save, rebase, check, squash, and publish the current feature PR.

    :param title: the final commit and PR title.
    :param path: a checkout directory, or None to use the current directory.
    :param body: the new PR description.
    :param timeout: the maximum seconds per check attempt.

    :return: final workspace, PR identity, and completed check attempts.
    """
    request = FinishInput(title=title, body=body)
    repository = _open_repository(path=path)
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        settings = read_settings(root=repository.root)
        hosting = GitHub(
            root=repository.root,
            url=repository.remotes.read_remote_url(remote=settings.git.remote),
        )
        recorder = CheckRecorder(root=repository.root, timeout=timeout)
        selected, state = publish_feature(
            repository=repository,
            hosting=hosting,
            settings=settings,
            request=request,
            execute=recorder.run_check,
        )
        return {
            "workspace": inspect_workspace(
                repository=repository, settings=settings, state=state
            ),
            "pr": selected.model_dump(),
            "checks": [
                result.model_dump(mode="json", exclude_unset=True)
                for result in recorder.results
            ],
        }


def release(
    *,
    path: Path | None = None,
    pr: int | None = None,
    tag: bool = False,
    delete_local_branch: bool = False,
    delete_remote_branch: bool = False,
    delete_worktree: bool = False,
) -> dict[str, object]:
    """Merge the checkout's PR and perform explicitly selected release actions.

    :param path: a task or primary checkout, or None to use the current directory.
    :param pr: the positive PR number; absent selects one PR for the current branch.
    :param tag: whether to publish the configured version tag.
    :param delete_local_branch: whether to delete the local PR branch.
    :param delete_remote_branch: whether to delete the remote PR branch.
    :param delete_worktree: whether to remove the PR's secondary worktree.

    :return: the merged PR identity, optional tag, and selected cleanup results.

    :raises OperationError: if the process directory is inside the Windows worktree
        selected for removal.
    """
    options = ReleaseOptions(
        number=pr,
        tag=tag,
        delete_local_branch=delete_local_branch,
        delete_remote_branch=delete_remote_branch,
        delete_worktree=delete_worktree,
    )
    repository = _open_repository(path=path)
    if (
        options.delete_worktree
        and os.name == "nt"
        and repository.root != repository.worktrees.list_checkouts()[0][0]
        and Path.cwd().resolve().is_relative_to(repository.root)
    ):
        raise OperationError(
            "Run release from outside the worktree and select it with --path.",
            code="invalid_request",
        )
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        settings = read_settings(root=repository.root)
        name, selected = release_pull_request(
            repository=repository,
            settings=settings,
            hosting=GitHub(
                root=repository.root,
                url=repository.remotes.read_remote_url(remote=settings.git.remote),
            ),
            options=options,
            today=datetime.now().astimezone().date(),
        )
        return {
            "pr": selected.model_dump(),
            "tag": name,
            "deleted_local_branch": options.delete_local_branch,
            "deleted_remote_branch": options.delete_remote_branch,
            "deleted_worktree": options.delete_worktree,
        }
