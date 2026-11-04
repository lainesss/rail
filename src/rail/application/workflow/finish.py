"""Finalize feature history and confirm its pull request publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import OperationError, WorkflowError
from rail.application.workflow.checks import run_checks


if TYPE_CHECKING:
    from collections.abc import Callable

    from rail.adapters.git.repository import Git, SourceState
    from rail.adapters.github import GitHub
    from rail.application.contracts import (
        Check,
        FinishInput,
        PullRequest,
        Settings,
    )


def publish_feature(
    *,
    repository: Git,
    hosting: GitHub,
    settings: Settings,
    request: FinishInput,
    execute: Callable[[Check], None],
) -> tuple[PullRequest, SourceState]:
    """Save work, rebase, check, squash, and publish one feature pull request.

    :param repository: the repository operations boundary.
    :param hosting: the pull request boundary.
    :param settings: the project workflow choices.
    :param request: the validated PR title, commit subject, and description.
    :param execute: the configured check execution boundary.

    :return: the confirmed PR and validated post-publication checkout state.

    :raises WorkflowError: if the branch is invalid, PR lookup is ambiguous,
        or files remain dirty before publication.
    :raises OperationError: if PR confirmation fails after publication or the
        published PR or checkout is inconsistent.
    """
    branch = repository.branches.read_branch()
    if branch == settings.git.main:
        raise WorkflowError("Finalization requires a feature branch.")
    if not repository.is_worktree_clean():
        repository.commits.commit_changes(title="wip")
    repository.remotes.refresh_remote(remote=settings.git.remote)
    main_commit = repository.remotes.read_remote_main_commit(
        main=settings.git.main, remote=settings.git.remote
    )
    matches = hosting.find_pull_requests(
        branch=branch, main=settings.git.main, state="open"
    )
    if len(matches) > 1:
        raise WorkflowError("Multiple open pull requests match this feature branch.")
    repository.commits.rebase_onto_main(main_commit=main_commit)
    run_checks(checks=settings.checks, execute=execute)
    repository.commits.squash_changes(main_commit=main_commit, title=request.title)
    head_commit = repository.commits.read_head_commit()
    if not repository.is_worktree_clean():
        raise WorkflowError("The working tree is still dirty; nothing was pushed.")
    repository.remotes.publish_branch(
        branch=branch, remote=settings.git.remote, commit=head_commit
    )
    try:
        if matches:
            selected = hosting.read_pull_request(
                number=matches[0].number, expected_head=head_commit
            )
        else:
            selected = hosting.create_pull_request(
                branch=branch,
                main=settings.git.main,
                title=request.title,
                body=request.body,
            )
            if selected.head_commit != head_commit:
                selected = hosting.read_pull_request(
                    number=selected.number, expected_head=head_commit
                )
    except (WorkflowError, ValueError) as error:
        raise OperationError(
            f"Push of commit {head_commit} to "
            f"{settings.git.remote}/{branch} succeeded, "
            f"but reading or creating the PR failed: {error}",
            code="publication_unconfirmed",
        ) from error
    state = repository.read_source_state(remote=settings.git.remote)
    if (
        selected.head != branch
        or selected.base != settings.git.main
        or selected.state != "OPEN"
    ):
        raise OperationError(
            f"Push succeeded, but PR #{selected.number} ({selected.url}) "
            f"has head branch {selected.head!r}, base {selected.base!r}, "
            f"and state {selected.state!r}; expected head branch {branch!r}, "
            f"base {settings.git.main!r}, and state 'OPEN'.",
            code="publication_mismatch",
        )
    if state.branch != branch or state.head != selected.head_commit:
        raise OperationError(
            f"Commit {selected.head_commit} was published to PR #{selected.number} "
            f"({selected.url}), but the local checkout changed during publication: "
            f"expected branch {branch!r} at {selected.head_commit}, "
            f"found branch {state.branch!r} at {state.head}. "
            "Local changes were preserved.",
            code="checkout_changed",
        )
    if state.dirty:
        raise OperationError(
            f"Commit {selected.head_commit} was published to PR #{selected.number} "
            f"({selected.url}), but local files changed during publication. "
            "These new edits are not part of the published commit. "
            "Run git status --short to inspect them; they were not discarded.",
            code="dirty_worktree",
        )
    return selected, state
