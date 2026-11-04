"""Exercise feature publication and consistency checks through the public API."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from rail.adapters.checks import CheckRecorder
from rail.adapters.git.repository import SourceState
from rail.adapters.github import GitHub
from rail.api import finish
from rail.application.contracts import Check, FinishInput, OperationError, PullRequest
from rail.application.workflow.finish import publish_feature
from tests.support.configuration import make_settings
from tests.support.repository import create_task_checkout


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tests.support.repository import Project


@pytest.mark.parametrize(argnames="reuse", argvalues=[False, True])
def test_finish_rereads_stale_pr_without_republishing(
    *, project: Project, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    """Context: publication to real local Git succeeds before gh reports the new head.
    Hosted PR lookup/creation and JSON responses are controlled test boundaries.

    Invariants:
        - Both new and reused PRs tolerate an initially stale head response.
        - The returned PR and remote branch identify the checked feature commit.
        - PR creation occurs only when no matching PR already exists.
    """
    task = create_task_checkout(project=project)
    old_head = task.commits.read_head_commit()
    (task.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    stale = PullRequest(
        number=1,
        url="https://github.com/example/project/pull/1",
        head=task.branches.read_branch(),
        base="main",
        state="OPEN",
        commit="",
        head_commit=old_head,
    )
    hosting = GitHub(root=task.root, url="https://github.com/example/project")
    create = Mock(return_value=stale)
    monkeypatch.setattr(
        target=hosting,
        name="find_pull_requests",
        value=Mock(return_value=[stale] if reuse else []),
    )
    monkeypatch.setattr(target=hosting, name="create_pull_request", value=create)
    reads = 0

    def run_gh_query(
        *, args: Sequence[str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        """Return one stale gh response followed by the actual published head.

        :param args: the requested GitHub CLI read arguments.
        :param cwd: the selected checkout.

        :return: the controlled JSON response for the PR read.
        """
        nonlocal reads
        reads += 1
        assert list(args[:4]) == ["gh", "pr", "view", "1"] and cwd == task.root
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 1,
                    "url": stale.url,
                    "headRefName": stale.head,
                    "baseRefName": "main",
                    "state": "OPEN",
                    "mergeCommit": None,
                    "isCrossRepository": False,
                    "headRefOid": (
                        old_head if reads == 1 else task.commits.read_head_commit()
                    ),
                }
            ),
        )

    pause = Mock()
    monkeypatch.setattr(target="rail.adapters.github.run_process", name=run_gh_query)
    monkeypatch.setattr(target="rail.adapters.github.sleep", name=pause)
    monkeypatch.setattr(target="rail.commands.GitHub", name=Mock(return_value=hosting))
    result = finish(path=task.root, title="Updated feature")
    published = task.commits.read_head_commit()
    assert published != old_head
    assert result["pr"]["head_commit"] == result["workspace"]["head"] == published
    assert task.run_command(
        "ls-remote", "--heads", "origin", "refs/heads/codex/task-17"
    ).stdout.startswith(published)
    assert reads == 2 and pause.call_count == 1
    assert create.call_count == int(not reuse)
    assert task.is_worktree_clean()


def test_finish_reports_files_changed_after_push(
    *, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a hosted response callback writes a new file after the real Git push.

    Invariants:
        - The failure identifies the published commit and newly uncommitted edits.
        - The remote retains the checked commit and the new local file is preserved.
    """
    task = create_task_checkout(project=project)
    (task.root / "feature.txt").write_text(data="checked feature", encoding="utf-8")
    hosting = Mock(spec=GitHub)
    hosting.find_pull_requests.return_value = []

    def create_pull_request(**_: str) -> PullRequest:
        """Write a local edit after publication and return GitHub's current PR head.

        :param _: the unused PR creation arguments.

        :return: the PR pointing to the published feature commit.
        """
        (task.root / "late-edit.txt").write_text(
            data="preserve this edit", encoding="utf-8"
        )
        return PullRequest(
            number=1,
            url="https://github.com/example/project/pull/1",
            head=task.branches.read_branch(),
            base="main",
            state="OPEN",
            commit="",
            head_commit=task.commits.read_head_commit(),
        )

    hosting.create_pull_request.side_effect = create_pull_request
    monkeypatch.setattr(target="rail.commands.GitHub", name=Mock(return_value=hosting))
    with pytest.raises(expected_exception=OperationError) as failure:
        finish(path=task.root, title="Checked feature")
    published = task.commits.read_head_commit()
    assert failure.value.code == "dirty_worktree"
    message = str(failure.value)
    assert published in message and "was published to PR #1" in message
    assert "local files changed during publication" in message
    assert (task.root / "late-edit.txt").read_text(
        encoding="utf-8"
    ) == "preserve this edit"
    assert task.run_command(
        "ls-remote", "--heads", "origin", "refs/heads/codex/task-17"
    ).stdout.startswith(published)
    hosting.create_pull_request.assert_called_once()


def test_finish_preserves_primary_and_returns_publication(
    *, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: primary main advances while a task has pending feature changes.

    Invariants:
        - Publication incorporates remote main without moving the primary checkout.
        - The returned clean workspace identifies the published PR commit.
        - No operation journal is produced.
    """
    task = create_task_checkout(project=project)
    (project.git.root / "main-only.txt").write_text(data="main", encoding="utf-8")
    project.git.commits.commit_changes(title="Advance main")
    project.git.run_command("push", "origin", "main")
    primary = project.git.commits.read_head_commit()
    (task.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    hosting = Mock(spec=GitHub)
    hosting.find_pull_requests.return_value = []

    def create_pull_request(**_: str) -> PullRequest:
        """Report the feature commit after the controlled hosted publication."""
        return PullRequest(
            number=1,
            url="https://github.com/example/project/pull/1",
            head=task.branches.read_branch(),
            base="main",
            state="OPEN",
            commit="",
            head_commit=task.commits.read_head_commit(),
        )

    hosting.create_pull_request.side_effect = create_pull_request
    monkeypatch.setattr(target="rail.commands.GitHub", name=Mock(return_value=hosting))
    result = finish(path=task.root, title="Task changes", body="Description")
    assert project.git.commits.read_head_commit() == primary
    assert (task.root / "main-only.txt").read_text(encoding="utf-8") == "main"
    assert task.is_worktree_clean()
    assert (
        result["workspace"]["head"]
        == result["pr"]["head_commit"]
        == task.commits.read_head_commit()
    )
    assert set(result) == {"workspace", "pr", "checks"} and result["checks"] == []
    assert not (task.read_common_directory() / "workflow-operations").exists()
    hosting.create_pull_request.assert_called_once_with(
        branch="codex/task-17", main="main", title="Task changes", body="Description"
    )


def test_recorded_checks_retry_and_failure_block_publication(
    *, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a check exhausts its retries before a later repaired publication.

    Invariants:
        - Failed attempts retain their exit statuses and preserve the wip commit.
        - Failure prevents publication; a repaired run publishes one clean commit.
    """
    task = create_task_checkout(project=project)
    (task.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    hosting = Mock(spec=GitHub)
    hosting.find_pull_requests.return_value = []
    recorder = CheckRecorder(root=task.root, timeout=30)
    settings = make_settings(
        checks=(
            Check(
                name="Required",
                args=(sys.executable, "-c", "raise SystemExit(1)"),
                retries=1,
            ),
        )
    )
    with pytest.raises(expected_exception=OperationError) as failure:
        publish_feature(
            repository=task,
            settings=settings,
            request=FinishInput(title="Task changes", body=""),
            hosting=hosting,
            execute=recorder.run_check,
        )
    assert failure.value.code == "check_failed"
    assert [attempt.exit_code for attempt in recorder.results] == [1, 1]
    assert task.run_command("log", "-1", "--format=%s").stdout.strip() == "wip"
    hosting.create_pull_request.assert_not_called()
    assert (
        task.run_command(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/remotes/origin/codex/task-17",
            allowed=(0, 1),
        ).returncode
        == 1
    )

    def create_pull_request(**_: str) -> PullRequest:
        """Return the observed local revision after publication succeeds."""
        return PullRequest(
            number=1,
            url="https://github.com/example/project/pull/1",
            head=task.branches.read_branch(),
            base="main",
            state="OPEN",
            commit="",
            head_commit=task.commits.read_head_commit(),
        )

    hosting.create_pull_request.side_effect = create_pull_request
    monkeypatch.setattr(target="rail.commands.GitHub", name=Mock(return_value=hosting))
    result = finish(path=task.root, title="Task changes")
    assert result["checks"] == [] and task.is_worktree_clean()
    assert (
        task.run_command("rev-list", "--count", "origin/main..HEAD").stdout.strip()
        == "1"
    )
    hosting.create_pull_request.assert_called_once()


@pytest.mark.parametrize(
    argnames="mismatch,code",
    argvalues=[
        ("pr", "publication_mismatch"),
        ("base", "publication_mismatch"),
        ("state", "publication_mismatch"),
        ("branch", "checkout_changed"),
        ("head", "checkout_changed"),
        ("dirty", "dirty_worktree"),
    ],
)
def test_finish_rejects_inconsistent_publication(
    *, tmp_path: Path, mismatch: str, code: str
) -> None:
    """Context: the resulting hosted PR or checkout differs from the published feature.

    Invariants:
        - Inconsistent publication data is never returned as success.
        - The error identifies the published PR and the specific mismatched state.
    """
    repository = Mock(root=tmp_path)
    repository.branches.read_branch.return_value = "feature"
    repository.is_worktree_clean.return_value = True
    repository.commits.read_head_commit.return_value = "a" * 40
    repository.read_common_directory.return_value = tmp_path
    repository.read_source_state.return_value = SourceState(
        branch="other" if mismatch == "branch" else "feature",
        head="b" * 40 if mismatch == "head" else "a" * 40,
        dirty=mismatch == "dirty",
    )
    selected = PullRequest(
        number=1,
        url="url",
        head="other" if mismatch == "pr" else "feature",
        base="trunk" if mismatch == "base" else "main",
        state="CLOSED" if mismatch == "state" else "OPEN",
        commit="",
        head_commit="a" * 40,
    )
    hosting = Mock(spec=GitHub)
    hosting.find_pull_requests.return_value = []
    hosting.create_pull_request.return_value = selected
    with pytest.raises(expected_exception=OperationError) as failure:
        publish_feature(
            repository=repository,
            hosting=hosting,
            settings=make_settings(),
            request=FinishInput(title="Feature", body=""),
            execute=Mock(),
        )
    assert failure.value.code == code
    message = str(failure.value)
    assert "PR #1" in message and selected.url in message
    if mismatch in {"pr", "base", "state"}:
        assert "Push succeeded" in message
        assert "'feature'" in message and "'main'" in message and "'OPEN'" in message
        assert {"pr": "'other'", "base": "'trunk'", "state": "'CLOSED'"}[mismatch] in (
            message
        )
    else:
        assert "a" * 40 in message and "was published" in message
        if mismatch == "head":
            assert "b" * 40 in message
        elif mismatch == "branch":
            assert "'other'" in message and "'feature'" in message
        else:
            assert "git status --short" in message and "new edits" in message
