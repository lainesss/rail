"""Coordinate checkout observations and creation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import OperationError


if TYPE_CHECKING:
    from pathlib import Path

    from rail.adapters.git.repository import Git, SourceState
    from rail.application.contracts import Settings


def inspect_workspace(
    *, repository: Git, settings: Settings, state: SourceState | None = None
) -> dict[str, object]:
    """Read checkout identity and source state without changing refs or files.

    :param repository: the discovered checkout.
    :param settings: the selected remote and integration branch.
    :param state: an existing observation to reuse, or None to read current state.

    :return: structured checkout metadata for an external caller.
    """
    return {
        "path": str(repository.root),
        "common_dir": str(repository.read_common_directory()),
        "main": settings.git.main,
        **(
            state
            if state is not None
            else repository.read_source_state(remote=settings.git.remote)
        ).model_dump(),
    }


def _validate_new_task_branch(*, repository: Git, name: str, main: str) -> None:
    """Validate a new task branch's literal name and availability.

    :param repository: the branch validation operations for the source checkout.
    :param name: the requested task branch name.
    :param main: the configured integration branch name.

    :raises OperationError: if the requested branch name is reserved or already used.
    """
    if name in {main, "main", "master", "HEAD"}:
        raise OperationError("Use a task branch name.", code="invalid_request")
    repository.branches.validate_branch(name=name)
    if repository.branches.read_branch_head(name=name) is not None:
        raise OperationError(f"Branch already exists: {name}", code="branch_exists")


def create_branch(*, repository: Git, settings: Settings, name: str) -> None:
    """Require a clean checkout and create a branch from the fetched configured base.

    :param repository: the repository command context.
    :param settings: the selected remote and integration branch.
    :param name: the new task branch name.

    :raises OperationError: if the source working tree has local changes.
    """
    if not repository.is_worktree_clean():
        raise OperationError(
            "The working tree must be clean before creating a branch.",
            code="dirty_worktree",
        )
    _validate_new_task_branch(repository=repository, name=name, main=settings.git.main)
    main_commit = repository.remotes.fetch_main_commit(
        main=settings.git.main, remote=settings.git.remote
    )
    repository.branches.create_branch(name=name, commit=main_commit)


def create_worktree(
    *, repository: Git, settings: Settings, path: Path, branch: str
) -> Git:
    """Create a new checkout and branch from the freshly fetched configured base.

    :param repository: the source checkout, whose files remain unchanged.
    :param settings: the selected remote and integration branch.
    :param path: the absolute destination for the new worktree.
    :param branch: the new task branch name.

    :return: the newly created checkout.

    :raises OperationError: if the destination is inside the source checkout or
        its Git metadata directory.
    """
    _validate_new_task_branch(
        repository=repository, name=branch, main=settings.git.main
    )
    if path.is_relative_to(repository.root) or path.is_relative_to(
        repository.read_common_directory()
    ):
        raise OperationError(
            "The worktree must be outside the source checkout and metadata."
        )
    main_commit = repository.remotes.fetch_main_commit(
        main=settings.git.main, remote=settings.git.remote
    )
    repository.worktrees.add_worktree(path=path, branch=branch, commit=main_commit)
    return repository.open_checkout(path=path)
