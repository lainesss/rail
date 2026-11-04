"""Select, validate, merge, tag, and clean up feature pull request releases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import WorkflowError
from rail.application.versioning import (
    parse_release_metadata,
    select_calver_tag_name,
    select_semver_tag_name,
)


if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from rail.adapters.git.repository import Git
    from rail.adapters.github import GitHub
    from rail.application.contracts import (
        GitSettings,
        PullRequest,
        ReleaseOptions,
        Settings,
    )


def release_pull_request(
    *,
    repository: Git,
    hosting: GitHub,
    settings: Settings,
    options: ReleaseOptions,
    today: date,
) -> tuple[str | None, PullRequest]:
    """Squash-merge a clean feature PR, optionally tag it, and perform cleanup.

    :param repository: the repository operations boundary.
    :param hosting: the pull request boundary.
    :param settings: the release and repository choices.
    :param options: the validated PR selection and requested tag and cleanup actions.
    :param today: the local calendar date used for calver.

    :return: the published tag, or None, and the merged pull request.

    :raises WorkflowError: if the checkout is dirty or a merged PR lacks its commit.
    """
    if not repository.is_worktree_clean():
        raise WorkflowError("Release requires a clean working tree.")
    branch = repository.branches.read_branch()
    selected = _select_pull_request(
        repository=repository,
        hosting=hosting,
        settings=settings.git,
        options=options,
        branch=branch,
    )
    primary, target = _prepare_release_checkouts(
        repository=repository,
        settings=settings.git,
        options=options,
        selected=selected,
        branch=branch,
    )
    main_commit = _refresh_main_commit(repository=repository, settings=settings.git)
    if options.delete_local_branch or options.delete_worktree:
        repository.branches.validate_main_update(
            main=settings.git.main, main_commit=main_commit
        )
    needs_merge = selected.state == "OPEN"
    if not needs_merge and not selected.commit:
        raise WorkflowError("The PR has no confirmed integration commit.")
    version_ref = (
        repository.commits.compute_merge_tree(
            main_commit=main_commit, feature_commit=selected.head_commit
        )
        if needs_merge
        else selected.commit
    )
    tag_name = (
        select_release_tag_name(
            repository=repository,
            settings=settings,
            version_ref=version_ref,
            commit=None if needs_merge else selected.commit,
            today=today,
        )
        if options.tag
        else None
    )
    if needs_merge:
        selected = hosting.merge_pull_request(
            number=selected.number, head_commit=selected.head_commit
        )
        main_commit = _refresh_main_commit(repository=repository, settings=settings.git)
    _validate_merged_pull_request(
        repository=repository,
        selected=selected,
        options=options,
        main_commit=main_commit,
    )
    if tag_name is not None:
        _publish_release_tag(
            repository=repository,
            settings=settings,
            selected=selected,
            name=tag_name,
            merged_now=needs_merge,
            today=today,
        )
    _run_release_cleanup(
        repository=repository,
        primary=primary,
        settings=settings.git,
        options=options,
        selected=selected,
        branch=branch,
        target=target,
    )
    return tag_name, selected


def _select_pull_request(
    *,
    repository: Git,
    hosting: GitHub,
    settings: GitSettings,
    options: ReleaseOptions,
    branch: str,
) -> PullRequest:
    """Select the requested PR or a unique matching open or completed branch PR.

    :param repository: the release checkout for matching completed PR heads.
    :param hosting: the PR lookup operations.
    :param settings: the configured integration branch and remote.
    :param options: the explicit PR number or branch lookup selection.
    :param branch: the release checkout's current branch.

    :return: the selected open or merged feature PR targeting configured main.

    :raises WorkflowError: if lookup is ambiguous or the PR is unsuitable for release.
    """
    if options.number is None:
        matches = hosting.find_pull_requests(
            branch=branch, main=settings.main, state="open"
        )
        if not matches:
            head = repository.commits.read_head_commit()
            matches = [
                candidate
                for candidate in hosting.find_pull_requests(
                    branch=branch, main=settings.main, state="merged"
                )
                if candidate.head_commit == head
            ]
        if len(matches) != 1:
            raise WorkflowError(
                f"No unique PR matches branch {branch!r}. Specify --pr NUMBER."
            )
        selected = matches[0]
    else:
        selected = hosting.read_pull_request(number=options.number)
    if selected.state not in {"OPEN", "MERGED"} or selected.base != settings.main:
        raise WorkflowError(
            "Release requires an open or merged PR targeting the configured main."
        )
    if selected.head == settings.main:
        raise WorkflowError("The release PR must identify a feature branch.")
    return selected


def _prepare_release_checkouts(
    *,
    repository: Git,
    settings: GitSettings,
    options: ReleaseOptions,
    selected: PullRequest,
    branch: str,
) -> tuple[Git, Path | None]:
    """Validate checkout identity and requested cleanup before merging or tagging.

    :param repository: the checkout selected for release.
    :param settings: the configured integration branch and remote.
    :param options: the validated PR selection and cleanup choices.
    :param selected: the feature PR selected for release.
    :param branch: the branch currently checked out in the release directory.

    :return: the primary checkout operations and the registered feature checkout path,
        or None for the feature checkout when its branch has no registered checkout.

    :raises WorkflowError: if checkout identity or cleanup choices are incompatible.
    """
    checkouts = repository.worktrees.list_checkouts()
    if not checkouts:
        raise WorkflowError("Release requires a registered checkout.")
    primary = repository.open_checkout(path=checkouts[0][0])
    secondary = repository.root != primary.root
    if branch != selected.head and not (
        branch == settings.main
        and (
            selected.state == "MERGED" or (not secondary and options.number is not None)
        )
    ):
        raise WorkflowError("The checkout branch does not match the release PR.")
    if not selected.head_commit:
        raise WorkflowError("The PR has no feature commit.")
    local_head = repository.branches.read_branch_head(name=selected.head)
    if local_head is not None and local_head != selected.head_commit:
        raise WorkflowError("The local feature branch differs from the PR head.")
    targets = [path for path, name in checkouts if name == selected.head]
    if options.delete_worktree and len(targets) > 1:
        raise WorkflowError(
            "Multiple worktrees use the PR branch; select cleanup manually."
        )
    target = targets[0] if targets else None
    if options.delete_worktree and target == primary.root:
        raise WorkflowError("Only a secondary worktree may be removed.")
    if options.delete_worktree and secondary and branch != selected.head:
        raise WorkflowError("Only the release PR's feature worktree may be removed.")
    if options.delete_worktree and target is not None:
        primary.worktrees.validate_worktree_removal(
            path=target, branch=selected.head, head=selected.head_commit
        )
    if options.delete_local_branch:
        for path, name in checkouts:
            if name == selected.head and (
                path != primary.root
                and not (options.delete_worktree and path == target)
            ):
                raise WorkflowError(
                    "Remove the feature worktree before deleting its local branch."
                )
        if (
            not secondary
            and branch == selected.head
            and any(
                name == settings.main and path != repository.root
                for path, name in checkouts
            )
        ):
            raise WorkflowError(
                "Main is checked out elsewhere; cannot switch this checkout."
            )
    if options.delete_local_branch or options.delete_worktree:
        for path, name in checkouts:
            if (
                name == settings.main
                and not repository.open_checkout(path=path).is_worktree_clean()
            ):
                raise WorkflowError("The main checkout must be clean before release.")
    return primary, target


def _refresh_main_commit(*, repository: Git, settings: GitSettings) -> str:
    """Fetch remote branches and read the refreshed integration commit.

    :param repository: the remote synchronization operations.
    :param settings: the configured integration branch and remote.

    :return: the refreshed remote main commit ID.
    """
    repository.remotes.refresh_remote(remote=settings.remote)
    return repository.remotes.read_remote_main_commit(
        main=settings.main, remote=settings.remote
    )


def _validate_merged_pull_request(
    *, repository: Git, selected: PullRequest, options: ReleaseOptions, main_commit: str
) -> None:
    """Require a confirmed integration commit and integrated feature changes.

    :param repository: the refreshed commit ancestry operations.
    :param selected: the hosted PR after merging or resolving a completed release.
    :param options: the requested branch and worktree cleanup.
    :param main_commit: the refreshed remote integration branch commit.

    :raises WorkflowError: if merge confirmation or feature changes are missing.
    """
    if selected.state != "MERGED" or not selected.commit:
        raise WorkflowError("The PR has no confirmed integration commit.")
    if not repository.commits.contains_commit(
        commit=selected.commit, main_commit=main_commit
    ):
        raise WorkflowError("The PR integration commit is absent from current main.")
    if (
        options.delete_local_branch
        or options.delete_remote_branch
        or options.delete_worktree
    ) and not (
        repository.commits.contains_commit(
            commit=selected.head_commit, main_commit=selected.commit
        )
        or repository.commits.is_squash_merge(
            commit=selected.head_commit, integration_commit=selected.commit
        )
    ):
        raise WorkflowError(
            "The PR changes are absent from the integration commit; cleanup refused."
        )


def select_release_tag_name(
    *,
    repository: Git,
    settings: Settings,
    version_ref: str,
    commit: str | None,
    today: date,
) -> str:
    """Validate and select a release tag before publishing any external changes.

    :param repository: the repository operations boundary.
    :param settings: the version policy, prefix, and publication remote.
    :param version_ref: the prospective merge tree or actual integration commit.
    :param commit: the actual integration commit, or None before merging.
    :param today: the local calendar date for a new calver tag.

    :return: the validated tag name, reusing a tag for the same integration commit.
    """
    existing = repository.tags.read_tags(remote=settings.git.remote)
    if settings.release.policy == "calver":
        name = select_calver_tag_name(
            prefix=settings.release.prefix,
            existing=existing,
            commit=commit,
            today=today,
        )
    else:
        version = parse_release_metadata(
            document=repository.commits.read_file(
                commit=version_ref, path="pyproject.toml"
            )
        ).project.version
        name = select_semver_tag_name(
            prefix=settings.release.prefix,
            version=version,
            existing=existing,
            commit=commit,
        )
    repository.tags.validate_tag(name=name)
    return name


def _publish_release_tag(
    *,
    repository: Git,
    settings: Settings,
    selected: PullRequest,
    name: str,
    merged_now: bool,
    today: date,
) -> None:
    """Recheck a newly merged version and publish its selected tag.

    :param repository: the release checkout and tag operations.
    :param settings: the version policy, prefix, and publication remote.
    :param selected: the confirmed merged PR and its integration commit.
    :param name: the tag selected during preflight.
    :param merged_now: whether this release invocation merged the PR.
    :param today: the local calendar date for calver selection.

    :raises WorkflowError: if the merged version differs from the preflight tag.
    """
    if merged_now:
        merged_tag = select_release_tag_name(
            repository=repository,
            settings=settings,
            version_ref=selected.commit,
            commit=selected.commit,
            today=today,
        )
        if name != merged_tag:
            raise WorkflowError(
                f"PR #{selected.number} was merged at {selected.commit}, but "
                f"preflight selected tag {name!r} and the merged state "
                f"requires {merged_tag!r}. No tag or cleanup was performed; "
                "rerun release with the PR number to inspect the merged state."
            )
    repository.tags.publish_tag(
        name=name, commit=selected.commit, remote=settings.git.remote
    )


def _run_release_cleanup(
    *,
    repository: Git,
    primary: Git,
    settings: GitSettings,
    options: ReleaseOptions,
    selected: PullRequest,
    branch: str,
    target: Path | None,
) -> None:
    """Run requested cleanup in dependency order and collect independent failures.

    :param repository: the release checkout used for remote deletion before removal.
    :param primary: the primary checkout retained throughout local cleanup.
    :param settings: the configured integration branch and remote.
    :param options: the requested branch and worktree deletions.
    :param selected: the confirmed merged PR and its authorized cleanup head.
    :param branch: the release checkout's original branch.
    :param target: the registered feature checkout, or None when already absent.

    :raises WorkflowError: if any requested cleanup step fails.
    """
    failures: list[tuple[str, Exception]] = []
    local_ready = True
    if options.delete_local_branch or options.delete_worktree:
        try:
            primary.update_main(main=settings.main, remote=settings.remote)
        except (WorkflowError, OSError) as error:
            failures.append(("update main", error))
            local_ready = False
    if options.delete_remote_branch:
        try:
            repository.branches.delete_remote_branch(
                name=selected.head,
                expected_head=selected.head_commit,
                remote=settings.remote,
            )
        except (WorkflowError, OSError) as error:
            failures.append(("delete remote branch", error))
    if options.delete_worktree and target is not None and local_ready:
        try:
            primary.worktrees.validate_worktree_removal(
                path=target, branch=selected.head, head=selected.head_commit
            )
            primary.worktrees.remove_worktree(path=target)
        except (WorkflowError, OSError) as error:
            failures.append(("remove worktree", error))
            local_ready = False
    if options.delete_local_branch and local_ready:
        try:
            if target == primary.root and branch == selected.head:
                primary.branches.switch_branch(name=settings.main)
            primary.branches.delete_local_branch(
                name=selected.head, expected_head=selected.head_commit
            )
        except (WorkflowError, OSError) as error:
            failures.append(("delete local branch", error))
    if failures:
        raise WorkflowError(
            "Release cleanup incomplete: "
            + "; ".join(f"{step}: {error}" for step, error in failures)
            + f". Retry with --path {primary.root} --pr {selected.number} "
            "and the same optional flags."
        ) from ExceptionGroup(
            "Release cleanup failures", [error for _, error in failures]
        )
