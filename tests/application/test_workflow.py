"""Exercise application policies with repository and hosting boundaries replaced."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from pydantic import ValidationError

from rail.adapters.git.repository import Git, SourceState
from rail.adapters.github import GitHub
from rail.application.contracts import (
    Check,
    CheckSettings,
    FinishInput,
    PullRequest,
    ReleaseOptions,
    WorkflowError,
)
from rail.application.workflow.checks import run_checks
from rail.application.workflow.finish import publish_feature
from rail.application.workflow.release import release_pull_request
from rail.application.workspaces import create_branch
from tests.support.configuration import make_settings


@pytest.fixture(name="repository")
def create_repository_mock() -> Mock:
    """Create a repository boundary with a clean feature checkout.

    :return: the configured repository test double.
    """
    result = Mock(spec=Git(root=Path.cwd()))
    result.root = Path("/project").resolve()
    result.worktrees.list_checkouts.return_value = [(result.root, "feature")]
    result.open_checkout.return_value = result
    result.branches.read_branch_head.return_value = "c" * 40
    result.is_worktree_clean.return_value = True
    result.branches.read_branch.return_value = "feature"
    result.remotes.read_remote_main_commit.return_value = "a" * 40
    result.commits.read_head_commit.return_value = "c" * 40
    result.commits.contains_commit.return_value = True
    result.commits.is_squash_merge.return_value = False
    result.tags.read_tags.return_value = {}
    result.commits.read_file.return_value = '[project]\nversion = "1.2.3"\n'
    result.read_source_state.return_value = SourceState(
        branch="feature", head="c" * 40, dirty=False
    )
    return result


@pytest.fixture(name="hosting")
def create_hosting_mock() -> Mock:
    """Create a hosting boundary for one merged feature.

    :return: the configured hosting test double.
    """
    result = Mock(spec=GitHub)
    selected = PullRequest(
        number=7,
        url="https://example.test/pr/7",
        head="feature",
        base="main",
        state="MERGED",
        commit="b" * 40,
        head_commit="c" * 40,
    )
    result.find_pull_requests.return_value = [selected]
    result.read_pull_request.return_value = selected
    result.merge_pull_request.return_value = selected
    result.create_pull_request.return_value = selected
    return result


@pytest.mark.parametrize(
    argnames="retries,failures,expected",
    argvalues=[
        (0, 0, 2),
        (1, 0, 2),
        (2, 0, 2),
        (0, 1, 1),
        (1, 1, 3),
        (1, 2, 2),
        (2, 1, 3),
        (2, 2, 4),
        (2, 3, 3),
    ],
)
def test_check_retry_and_fail_fast(
    *, retries: int, failures: int, expected: int
) -> None:
    """Context: configured commands encounter controlled execution failures.

    Invariants:
        - Commands execute in order and stop after a definitive failure.
        - Only the failed command repeats within its own retry allowance.
    """
    execute = Mock(
        side_effect=[*[WorkflowError("failed") for _ in range(failures)], None, None]
    )
    first = Check(name="First", args=("first",), retries=retries)
    second = Check(name="Second", args=("second",), retries=0)
    checks = CheckSettings(commands=(first, second))
    if failures > retries:
        with pytest.raises(expected_exception=WorkflowError, match="failed"):
            run_checks(checks=checks, execute=execute)
    else:
        run_checks(checks=checks, execute=execute)
    assert execute.call_count == expected
    assert execute.call_args_list[0] == call(first)
    if failures <= retries:
        assert execute.call_args_list[-1] == call(second)
        assert execute.call_args_list.count(call(second)) == 1
    else:
        assert all(invocation == call(first) for invocation in execute.call_args_list)


def test_retry_allowances_are_independent() -> None:
    """Context: three named checks have different retry counts and controlled failures.

    Invariants:
        - Each command uses only its own retry count.
        - Successful earlier checks are never repeated.
        - A later command has its full retry allowance after earlier retries.
    """
    checks = (
        Check(name="First", args=("first",), retries=0),
        Check(name="Second", args=("second",), retries=2),
        Check(name="Third", args=("third",), retries=1),
    )
    execute = Mock(
        side_effect=[
            None,
            WorkflowError("second"),
            WorkflowError("second"),
            None,
            WorkflowError("third"),
            None,
        ]
    )
    run_checks(checks=CheckSettings(commands=checks), execute=execute)
    assert execute.call_args_list == [
        call(checks[0]),
        call(checks[1]),
        call(checks[1]),
        call(checks[1]),
        call(checks[2]),
        call(checks[2]),
    ]


def test_unexpected_failure_is_not_retried() -> None:
    """Context: a check boundary raises an unexpected programming error.

    Invariants:
        - The unexpected exception propagates after one attempt.
    """
    execute = Mock(side_effect=ValueError("unexpected"))
    with pytest.raises(expected_exception=ValueError, match="unexpected"):
        run_checks(
            checks=CheckSettings(
                commands=(Check(name="Script", args=("script",), retries=2),)
            ),
            execute=execute,
        )
    execute.assert_called_once()


def test_empty_checks() -> None:
    """Context: the project configures no checks.

    Invariants:
        - No execution is requested.
    """
    execute = Mock()
    run_checks(checks=CheckSettings(commands=()), execute=execute)
    execute.assert_not_called()


def test_create_branch_preserves_checkout(*, repository: Mock) -> None:
    """Context: a clean repository creates a feature with custom Git settings.

    Invariants:
        - Cleanliness and branch availability are established before creation.
        - Creation receives the configured remote and integration branch.
        - Neither local main nor the checked-out branch is changed.
    """
    repository.branches.read_branch_head.return_value = None
    create_branch(
        repository=repository,
        settings=make_settings(remote="central", main="integration/trunk"),
        name="feature",
    )
    assert repository.mock_calls == [
        call.is_worktree_clean(),
        call.branches.validate_branch(name="feature"),
        call.branches.read_branch_head(name="feature"),
        call.remotes.fetch_main_commit(main="integration/trunk", remote="central"),
        call.branches.create_branch(
            name="feature", commit=repository.remotes.fetch_main_commit.return_value
        ),
    ]


@pytest.mark.parametrize(argnames="state", argvalues=["dirty", "occupied", "reserved"])
def test_create_branch_rejects_invalid_state(*, repository: Mock, state: str) -> None:
    """Context: branch creation encounters dirty files or an unavailable name.

    Invariants:
        - The operation fails before creating or switching a branch.
    """
    repository.is_worktree_clean.return_value = state != "dirty"
    repository.branches.read_branch_head.return_value = (
        "c" * 40 if state == "occupied" else None
    )
    with pytest.raises(expected_exception=WorkflowError):
        create_branch(
            repository=repository,
            settings=make_settings(remote="central", main="integration/trunk"),
            name="integration/trunk" if state == "reserved" else "feature",
        )
    repository.branches.create_branch.assert_not_called()
    repository.branches.switch_branch.assert_not_called()


@pytest.mark.parametrize(argnames="reuse", argvalues=[True, False])
def test_finish_sequence(*, repository: Mock, hosting: Mock, reuse: bool) -> None:
    """Context: finalization saves dirty input with nonmodifying checks and mocked Git.

    Invariants:
        - Wip preservation precedes rebase and checks.
        - Squash follows successful checks and precedes publication.
        - An existing PR is reused; otherwise creation receives the supplied body.
    """
    selected = PullRequest.model_validate(
        {
            **hosting.read_pull_request.return_value.model_dump(),
            "state": "OPEN",
            "commit": "",
        }
    )
    hosting.read_pull_request.return_value = (
        hosting.create_pull_request.return_value
    ) = selected
    hosting.find_pull_requests.return_value = [selected]
    repository.is_worktree_clean.side_effect = [False, True]
    if not reuse:
        hosting.find_pull_requests.return_value = []
    calls = Mock()
    calls.attach_mock(mock=repository, attribute="repository")
    execute = Mock()
    calls.attach_mock(mock=execute, attribute="run_check")
    result, _ = publish_feature(
        repository=repository,
        hosting=hosting,
        settings=make_settings(
            checks=(Check(name="Script", args=("script",), retries=1),)
        ),
        request=FinishInput(title="Feature", body="line 1\nline 2"),
        execute=execute,
    )
    assert result.url == "https://example.test/pr/7"
    assert calls.mock_calls.index(
        call.repository.commits.commit_changes(title="wip")
    ) < calls.mock_calls.index(
        call.repository.commits.rebase_onto_main(main_commit="a" * 40)
    )
    assert calls.mock_calls.index(
        call.repository.commits.rebase_onto_main(main_commit="a" * 40)
    ) < calls.mock_calls.index(
        call.run_check(Check(name="Script", args=("script",), retries=1))
    )
    assert calls.mock_calls.index(
        call.run_check(Check(name="Script", args=("script",), retries=1))
    ) < calls.mock_calls.index(
        call.repository.commits.squash_changes(main_commit="a" * 40, title="Feature")
    )
    assert calls.mock_calls.index(
        call.repository.commits.squash_changes(main_commit="a" * 40, title="Feature")
    ) < calls.mock_calls.index(
        call.repository.remotes.publish_branch(
            branch="feature", remote="origin", commit="c" * 40
        )
    )
    assert hosting.create_pull_request.call_count == int(not reuse)
    if not reuse:
        hosting.create_pull_request.assert_called_once_with(
            branch="feature", main="main", title="Feature", body="line 1\nline 2"
        )
    else:
        hosting.read_pull_request.assert_called_once_with(
            number=7, expected_head="c" * 40
        )


@pytest.mark.parametrize(argnames="reuse", argvalues=[False, True])
def test_finish_reports_hosted_failure_after_successful_push(
    *, repository: Mock, hosting: Mock, reuse: bool
) -> None:
    """Context: a PR API operation fails after the feature push has succeeded.

    Invariants:
        - The error identifies the successful push, exact commit, branch, and API error.
        - Publication is attempted once; the API failure does not repeat the push.
    """
    if not reuse:
        hosting.find_pull_requests.return_value = []
    hosting.read_pull_request.side_effect = WorkflowError("GitHub read unavailable")
    hosting.create_pull_request.side_effect = WorkflowError("GitHub create unavailable")
    with pytest.raises(expected_exception=WorkflowError) as failure:
        publish_feature(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            request=FinishInput(title="Feature", body=""),
            execute=Mock(),
        )
    message = str(failure.value)
    assert "Push" in message and "succeeded" in message and "c" * 40 in message
    assert (
        "origin/feature" in message and "GitHub" in message and "unavailable" in message
    )
    repository.remotes.publish_branch.assert_called_once_with(
        branch="feature", remote="origin", commit="c" * 40
    )


def test_finish_clean_input(*, repository: Mock, hosting: Mock) -> None:
    """Context: finalization has clean input and nonmodifying checks.

    Invariants:
        - No wip commit is requested; checked history is squashed.
        - Publication returns the matching PR URL.
    """
    selected = PullRequest.model_validate(
        {
            **hosting.read_pull_request.return_value.model_dump(),
            "state": "OPEN",
            "commit": "",
        }
    )
    hosting.read_pull_request.return_value = selected
    hosting.find_pull_requests.return_value = [selected]
    result, _ = publish_feature(
        repository=repository,
        hosting=hosting,
        settings=make_settings(),
        request=FinishInput(title="Feature", body=""),
        execute=Mock(),
    )
    assert result == selected
    repository.commits.commit_changes.assert_not_called()
    repository.commits.squash_changes.assert_called_once_with(
        main_commit="a" * 40, title="Feature"
    )


@pytest.mark.parametrize(
    argnames="state",
    argvalues=["main", "ambiguous", "dirty", "check"],
)
def test_finish_rejections(*, repository: Mock, hosting: Mock, state: str) -> None:
    """Context: finalization encounters an invalid state or definitive check failure.

    Invariants:
        - No feature push or PR creation occurs after the failure.
    """
    if state == "main":
        repository.branches.read_branch.return_value = "main"
    if state == "ambiguous":
        hosting.find_pull_requests.return_value *= 2
    if state == "dirty":
        repository.is_worktree_clean.side_effect = [True, False]
    execute = Mock(
        side_effect=WorkflowError("check failed") if state == "check" else None
    )
    with pytest.raises(expected_exception=WorkflowError):
        publish_feature(
            repository=repository,
            hosting=hosting,
            settings=make_settings(
                checks=(Check(name="Script", args=("script",), retries=1),)
            ),
            request=FinishInput(title="Feature", body=""),
            execute=execute,
        )
    repository.remotes.publish_branch.assert_not_called()
    hosting.create_pull_request.assert_not_called()
    if state == "check":
        repository.commits.squash_changes.assert_not_called()


@pytest.mark.parametrize(argnames="number", argvalues=[None, 7])
def test_release_targets_pr_commit(
    *, repository: Mock, hosting: Mock, number: int | None
) -> None:
    """Context: main has advanced beyond a merged PR's integration commit.

    Invariants:
        - Version lookup and tagging use the PR integration commit.
        - The refreshed main tip is used only for containment and synchronization.
    """
    tag, selected = release_pull_request(
        repository=repository,
        hosting=hosting,
        settings=make_settings(),
        options=ReleaseOptions(
            number=number,
            tag=True,
            delete_local_branch=False,
            delete_remote_branch=False,
            delete_worktree=False,
        ),
        today=date(year=2026, month=10, day=24),
    )
    assert tag == "v1.2.3"
    repository.commits.read_file.assert_called_once_with(
        commit=selected.commit, path="pyproject.toml"
    )
    repository.commits.contains_commit.assert_called_once_with(
        commit=selected.commit, main_commit="a" * 40
    )
    repository.tags.publish_tag.assert_called_once_with(
        name=tag, commit=selected.commit, remote="origin"
    )
    repository.branches.switch_branch.assert_not_called()


@pytest.mark.parametrize(argnames="lookup", argvalues=["open", "merged", "missing"])
def test_release_lookup_uses_current_branch(
    *, repository: Mock, hosting: Mock, lookup: str
) -> None:
    """Context: PR lookup sees an open PR or older merged PRs for the current branch.

    Invariants:
        - An open branch PR is selected without querying historical matches.
        - A retry selects only a merged PR whose head equals the current commit.
        - Unrelated historical commits require an explicit PR number.
    """
    merged = hosting.read_pull_request.return_value
    unrelated = PullRequest.model_validate(
        {
            **merged.model_dump(),
            "number": 6,
            "head_commit": "d" * 40,
        }
    )
    if lookup == "open":
        hosting.find_pull_requests.side_effect = [
            [
                PullRequest.model_validate(
                    obj={**merged.model_dump(), "state": "OPEN", "commit": ""}
                )
            ]
        ]
    else:
        hosting.find_pull_requests.side_effect = [
            [],
            [unrelated, merged] if lookup == "merged" else [unrelated],
        ]
    if lookup == "missing":
        with pytest.raises(expected_exception=WorkflowError, match="--pr NUMBER"):
            release_pull_request(
                repository=repository,
                hosting=hosting,
                settings=make_settings(),
                options=ReleaseOptions(
                    number=None,
                    tag=False,
                    delete_local_branch=False,
                    delete_remote_branch=False,
                    delete_worktree=False,
                ),
                today=date(year=2026, month=10, day=28),
            )
        hosting.merge_pull_request.assert_not_called()
    else:
        _, selected = release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=None,
                tag=False,
                delete_local_branch=False,
                delete_remote_branch=False,
                delete_worktree=False,
            ),
            today=date(year=2026, month=10, day=28),
        )
        assert (
            selected.number == 7
            and selected.head_commit == repository.commits.read_head_commit.return_value
        )
    expected = [call(branch="feature", main="main", state="open")]
    if lookup != "open":
        expected.append(call(branch="feature", main="main", state="merged"))
    assert hosting.find_pull_requests.call_args_list == expected
    hosting.read_pull_request.assert_not_called()


def test_release_detects_version_changed_after_preflight(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: controlled merged metadata differs from the preflight version.

    Invariants:
        - The merged PR is identified in the failure with both planned tag names.
        - Tag publication and branch cleanup stop rather than using a different tag.
    """
    hosting.read_pull_request.return_value = PullRequest.model_validate(
        {
            **hosting.read_pull_request.return_value.model_dump(),
            "state": "OPEN",
            "commit": "",
        }
    )
    repository.commits.read_file.side_effect = [
        '[project]\nversion = "1.2.3"\n',
        '[project]\nversion = "2.0.0"\n',
    ]
    with pytest.raises(expected_exception=WorkflowError) as failure:
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=7,
                tag=True,
                delete_local_branch=False,
                delete_remote_branch=True,
                delete_worktree=False,
            ),
            today=date(year=2026, month=10, day=28),
        )
    message = str(failure.value)
    assert "was merged" in message and "v1.2.3" in message and "v2.0.0" in message
    hosting.merge_pull_request.assert_called_once()
    repository.tags.publish_tag.assert_not_called()
    repository.branches.delete_local_branch.assert_not_called()
    repository.branches.delete_remote_branch.assert_not_called()


@pytest.mark.parametrize(argnames="invalid", argvalues=["state", "commit"])
def test_release_rejects_unconfirmed_merge(
    *, repository: Mock, hosting: Mock, invalid: str
) -> None:
    """Context: controlled hosting returns an invalid result after the merge request.

    Invariants:
        - Release reports missing integration confirmation and stops cleanup.
    """
    hosting.read_pull_request.return_value = PullRequest.model_validate(
        {
            **hosting.read_pull_request.return_value.model_dump(),
            "state": "OPEN",
            "commit": "",
        }
    )
    merged = hosting.merge_pull_request.return_value
    hosting.merge_pull_request.return_value = PullRequest.model_validate(
        {
            **merged.model_dump(),
            "state": "OPEN" if invalid == "state" else "MERGED",
            "commit": merged.commit if invalid == "state" else "",
        }
    )
    with pytest.raises(expected_exception=WorkflowError, match="confirmed integration"):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=7,
                tag=False,
                delete_local_branch=False,
                delete_remote_branch=True,
                delete_worktree=False,
            ),
            today=date(year=2026, month=10, day=28),
        )
    hosting.merge_pull_request.assert_called_once()
    repository.branches.delete_local_branch.assert_not_called()
    repository.branches.delete_remote_branch.assert_not_called()


def test_calver_release_does_not_read_project_version(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: the release workflow uses calendar versioning with mocked Git.

    Invariants:
        - The calendar tag is published at the PR integration commit.
        - Project version lookup is unnecessary for calver.
    """
    result, _ = release_pull_request(
        repository=repository,
        hosting=hosting,
        settings=make_settings(policy="calver", prefix=""),
        options=ReleaseOptions(
            number=7,
            tag=True,
            delete_local_branch=False,
            delete_remote_branch=False,
            delete_worktree=False,
        ),
        today=date(year=2026, month=10, day=24),
    )
    assert result == "2026.10.24"
    repository.tags.publish_tag.assert_called_once_with(
        name="2026.10.24", commit="b" * 40, remote="origin"
    )
    repository.commits.read_file.assert_not_called()


@pytest.mark.parametrize(
    argnames="state",
    argvalues=[
        "dirty",
        "missing",
        "ambiguous",
        "closed",
        "base",
        "head",
        "commit",
        "absent",
        "version",
        "duplicate",
    ],
)
def test_release_rejections(*, repository: Mock, hosting: Mock, state: str) -> None:
    """Context: release identification, merge state, ancestry, or tags are invalid.

    Invariants:
        - Invalid release state never creates or pushes a tag.
    """
    repository.is_worktree_clean.return_value = state != "dirty"
    if state == "missing":
        hosting.find_pull_requests.return_value = []
    if state == "ambiguous":
        hosting.find_pull_requests.return_value *= 2
    if state in {"closed", "base", "head", "commit"}:
        hosting.find_pull_requests.return_value = [
            PullRequest(
                number=7,
                url="url",
                head="main" if state == "head" else "feature",
                base="other" if state == "base" else "main",
                state="CLOSED" if state == "closed" else "MERGED",
                commit="" if state == "commit" else "b" * 40,
                head_commit="c" * 40,
            )
        ]
    repository.commits.contains_commit.return_value = state != "absent"
    version = "1.2" if state == "version" else "1.2.3"
    repository.commits.read_file.return_value = f'[project]\nversion = "{version}"\n'
    repository.tags.read_tags.return_value = (
        {"v1.2.3": "d" * 40} if state == "duplicate" else {}
    )
    expected_error = ValidationError if state == "version" else WorkflowError
    with pytest.raises(expected_exception=expected_error):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=None,
                tag=True,
                delete_local_branch=False,
                delete_remote_branch=False,
                delete_worktree=False,
            ),
            today=date(year=2026, month=1, day=1),
        )
    repository.tags.publish_tag.assert_not_called()


def test_release_merges_without_optional_actions(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: release is called on an open PR without optional actions.

    Invariants:
        - The exact PR head is merged without tagging or deleting anything.
    """
    hosting.read_pull_request.return_value = PullRequest(
        number=7,
        url="url",
        head="feature",
        base="main",
        state="OPEN",
        commit="",
        head_commit="c" * 40,
    )
    name, selected = release_pull_request(
        repository=repository,
        hosting=hosting,
        settings=make_settings(),
        options=ReleaseOptions(
            number=7,
            tag=False,
            delete_local_branch=False,
            delete_remote_branch=False,
            delete_worktree=False,
        ),
        today=date(year=2026, month=10, day=28),
    )
    assert name is None and selected.state == "MERGED"
    hosting.merge_pull_request.assert_called_once_with(number=7, head_commit="c" * 40)
    repository.tags.read_tags.assert_not_called()
    repository.branches.delete_local_branch.assert_not_called()
    repository.branches.delete_remote_branch.assert_not_called()
    repository.worktrees.remove_worktree.assert_not_called()


def test_release_cleans_from_primary_after_removal(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: a secondary task checkout is released with every action enabled.

    Invariants:
        - Tag publication precedes remote deletion and worktree removal.
        - Main update precedes removal and local deletion uses the primary checkout.
    """
    primary = Mock(spec=Git(root=Path.cwd()))
    primary.root = repository.root.parent / "primary"
    primary.is_worktree_clean.return_value = True
    repository.worktrees.list_checkouts.return_value = [
        (primary.root, "main"),
        (repository.root, "feature"),
    ]
    repository.open_checkout.return_value = primary
    effects = Mock()
    effects.attach_mock(repository.tags.publish_tag, "publish_tag")
    effects.attach_mock(repository.branches.delete_remote_branch, "remote_delete")
    effects.attach_mock(primary.worktrees.remove_worktree, "remove")
    effects.attach_mock(primary.update_main, "update")
    effects.attach_mock(primary.branches.delete_local_branch, "local_delete")
    release_pull_request(
        repository=repository,
        hosting=hosting,
        settings=make_settings(),
        options=ReleaseOptions(
            number=7,
            tag=True,
            delete_local_branch=True,
            delete_remote_branch=True,
            delete_worktree=True,
        ),
        today=date(year=2026, month=10, day=28),
    )
    assert effects.mock_calls == [
        call.publish_tag(name="v1.2.3", commit="b" * 40, remote="origin"),
        call.update(main="main", remote="origin"),
        call.remote_delete(name="feature", expected_head="c" * 40, remote="origin"),
        call.remove(path=repository.root),
        call.local_delete(name="feature", expected_head="c" * 40),
    ]


def test_release_rejects_retained_feature_worktree(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: local branch deletion is requested while its worktree is retained.

    Invariants:
        - The incompatible choice is rejected before merging or deleting anything.
    """
    primary = Mock(spec=Git(root=Path.cwd()))
    primary.root = repository.root.parent / "primary"
    repository.worktrees.list_checkouts.return_value = [
        (primary.root, "main"),
        (repository.root, "feature"),
    ]
    repository.open_checkout.return_value = primary
    with pytest.raises(expected_exception=WorkflowError, match="worktree"):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=7,
                tag=False,
                delete_local_branch=True,
                delete_remote_branch=False,
                delete_worktree=False,
            ),
            today=date(year=2026, month=10, day=28),
        )
    hosting.merge_pull_request.assert_not_called()
    repository.branches.delete_local_branch.assert_not_called()
    repository.branches.delete_remote_branch.assert_not_called()


def test_release_tag_failure_stops_cleanup(*, repository: Mock, hosting: Mock) -> None:
    """Context: the selected version cannot be published after a successful merge.

    Invariants:
        - Remote and local cleanup do not run after the tag failure.
    """
    repository.tags.publish_tag.side_effect = WorkflowError("push failed")
    with pytest.raises(expected_exception=WorkflowError, match="push failed"):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=7,
                tag=True,
                delete_local_branch=True,
                delete_remote_branch=True,
                delete_worktree=False,
            ),
            today=date(year=2026, month=10, day=28),
        )
    repository.branches.delete_local_branch.assert_not_called()
    repository.branches.delete_remote_branch.assert_not_called()
    repository.branches.switch_branch.assert_not_called()


def test_failed_main_update_does_not_block_remote_cleanup(
    *,
    repository: Mock,
    hosting: Mock,
) -> None:
    """Context: local main synchronization fails after confirmed publication.

    Invariants:
        - Remote cleanup proceeds while local branch deletion stays blocked.
        - The synchronization failure remains visible to the caller.
    """
    repository.update_main.side_effect = WorkflowError("sync failed")
    with pytest.raises(expected_exception=WorkflowError, match="sync failed"):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=7,
                tag=False,
                delete_local_branch=True,
                delete_remote_branch=True,
                delete_worktree=False,
            ),
            today=date(year=2026, month=11, day=2),
        )
    repository.branches.delete_remote_branch.assert_called_once_with(
        name="feature", expected_head="c" * 40, remote="origin"
    )
    repository.branches.switch_branch.assert_not_called()


@pytest.mark.parametrize(
    argnames="condition",
    argvalues=[
        "branch",
        "head",
        "local-head",
        "checkouts",
        "primary",
        "other-worktree",
        "main-dirty",
        "main-occupied",
        "multiple",
    ],
)
def test_release_preflight_preserves_pr(
    *, repository: Mock, hosting: Mock, condition: str
) -> None:
    """Context: release has an incompatible checkout or cleanup choice.

    Invariants:
        - The invalid state is rejected before merge, tag, or deletion.
    """
    selected = hosting.read_pull_request.return_value
    options = {}
    if condition == "branch":
        repository.branches.read_branch.return_value = "unrelated"
    elif condition == "head":
        hosting.read_pull_request.return_value = PullRequest.model_validate(
            {
                **selected.model_dump(),
                "head_commit": "",
            }
        )
    elif condition == "local-head":
        repository.branches.read_branch_head.return_value = "d" * 40
    elif condition == "checkouts":
        repository.worktrees.list_checkouts.return_value = []
    elif condition == "primary":
        options = {"delete_worktree": True}
    elif condition == "multiple":
        repository.worktrees.list_checkouts.return_value.append(
            (repository.root.parent / "other", "feature")
        )
        options = {"delete_worktree": True}
    elif condition == "main-occupied":
        repository.worktrees.list_checkouts.return_value.append(
            (repository.root.parent / "main", "main")
        )
        options = {"delete_local_branch": True}
    else:
        primary = Mock(spec=Git(root=Path.cwd()))
        primary.root = repository.root.parent / "primary"
        primary.is_worktree_clean.return_value = condition != "main-dirty"
        repository.open_checkout.return_value = primary
        repository.worktrees.list_checkouts.return_value = [
            (primary.root, "main"),
            (repository.root, "feature"),
        ]
        options = {"delete_worktree": True}
        if condition == "other-worktree":
            repository.branches.read_branch.return_value = "main"
    release_options = ReleaseOptions.model_validate(
        obj={
            "number": 7,
            "tag": False,
            "delete_local_branch": False,
            "delete_remote_branch": False,
            "delete_worktree": False,
            **options,
        },
    )
    with pytest.raises(expected_exception=WorkflowError):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=release_options,
            today=date(year=2026, month=10, day=28),
        )
    hosting.merge_pull_request.assert_not_called()
    repository.tags.publish_tag.assert_not_called()
    repository.branches.delete_local_branch.assert_not_called()
    repository.branches.delete_remote_branch.assert_not_called()


def test_release_stops_changed_worktree_cleanup(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: the feature checkout changes after merge but before removal.

    Invariants:
        - Cleanup refuses to remove the changed checkout or local branch.
    """
    primary = Mock(spec=Git(root=Path.cwd()))
    primary.root = repository.root.parent / "primary"
    primary.is_worktree_clean.return_value = True
    repository.open_checkout.return_value = primary
    repository.worktrees.list_checkouts.return_value = [
        (primary.root, "main"),
        (repository.root, "feature"),
    ]
    primary.worktrees.validate_worktree_removal.side_effect = [
        None,
        WorkflowError("The feature worktree changed during release"),
    ]
    with pytest.raises(
        expected_exception=WorkflowError, match="changed during release"
    ):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=7,
                tag=False,
                delete_local_branch=False,
                delete_remote_branch=False,
                delete_worktree=True,
            ),
            today=date(year=2026, month=10, day=28),
        )
    primary.worktrees.remove_worktree.assert_not_called()
    primary.branches.delete_local_branch.assert_not_called()


def test_release_refuses_cleanup_of_unmerged_head(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: the current feature head is absent from the PR's integration commit.

    Invariants:
        - The remote branch is retained rather than deleting unmerged history.
    """
    repository.commits.contains_commit.side_effect = [True, False]
    with pytest.raises(expected_exception=WorkflowError, match="cleanup refused"):
        release_pull_request(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            options=ReleaseOptions(
                number=7,
                tag=False,
                delete_local_branch=False,
                delete_remote_branch=True,
                delete_worktree=False,
            ),
            today=date(year=2026, month=10, day=28),
        )
    repository.branches.delete_local_branch.assert_not_called()
    repository.branches.delete_remote_branch.assert_not_called()


def test_release_allows_cleanup_of_squashed_head(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: the PR's changes are squash-merged without retaining its head SHA.

    Invariants:
        - Verified squash integration permits cleanup of the exact feature head.
    """
    repository.commits.contains_commit.side_effect = [True, False]
    repository.commits.is_squash_merge.return_value = True
    release_pull_request(
        repository=repository,
        hosting=hosting,
        settings=make_settings(),
        options=ReleaseOptions(
            number=7,
            tag=False,
            delete_local_branch=False,
            delete_remote_branch=True,
            delete_worktree=False,
        ),
        today=date(year=2026, month=10, day=28),
    )
    repository.commits.is_squash_merge.assert_called_once_with(
        commit="c" * 40, integration_commit="b" * 40
    )
    repository.branches.delete_remote_branch.assert_called_once_with(
        name="feature", expected_head="c" * 40, remote="origin"
    )


def test_release_switches_primary_before_local_deletion(
    *, repository: Mock, hosting: Mock
) -> None:
    """Context: release runs in the primary feature checkout with local deletion.

    Invariants:
        - Main is updated and checked out before deleting the completed feature.
    """
    effects = Mock()
    effects.attach_mock(repository.update_main, "update")
    effects.attach_mock(repository.branches.switch_branch, "switch_branch")
    effects.attach_mock(repository.branches.delete_local_branch, "delete")
    release_pull_request(
        repository=repository,
        hosting=hosting,
        settings=make_settings(),
        options=ReleaseOptions(
            number=7,
            tag=False,
            delete_local_branch=True,
            delete_remote_branch=False,
            delete_worktree=False,
        ),
        today=date(year=2026, month=10, day=28),
    )
    assert effects.mock_calls == [
        call.update(main="main", remote="origin"),
        call.switch_branch(name="main"),
        call.delete(name="feature", expected_head="c" * 40),
    ]


def test_calver_retry_reuses_previous_date(*, repository: Mock, hosting: Mock) -> None:
    """Context: a release is resumed on the day after its tag was published.

    Invariants:
        - The original integration commit's tag is reused without a new version.
    """
    repository.tags.read_tags.return_value = {"v2026.10.27": "b" * 40}
    name, _ = release_pull_request(
        repository=repository,
        hosting=hosting,
        settings=make_settings(policy="calver"),
        options=ReleaseOptions(
            number=7,
            tag=True,
            delete_local_branch=False,
            delete_remote_branch=False,
            delete_worktree=False,
        ),
        today=date(year=2026, month=10, day=28),
    )
    assert name == "v2026.10.27"
