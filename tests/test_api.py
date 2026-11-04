"""Exercise the public Python API against isolated Git repositories."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from rail import api
from rail.adapters.git.repository import Git
from rail.adapters.github import GitHub
from rail.application.contracts import OperationError, PullRequest
from tests.support.configuration import make_settings, write_settings
from tests.support.repository import create_task_checkout


if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.repository import Project


def test_public_exports() -> None:
    """Context: a consumer imports the supported library entry point.

    Invariants:
        - The public export list contains only the eight command functions.
    """
    assert set(api.__all__) == {
        "install",
        "inspect",
        "fetch",
        "create_branch",
        "create_worktree",
        "check",
        "finish",
        "release",
    }
    assert all(callable(getattr(api, name)) for name in api.__all__)


def test_install_fetch_and_branch(*, git_project: Git) -> None:
    """Context: a library consumer configures a repository and creates a feature.

    Invariants:
        - Reinstallation preserves existing settings.
        - Fetch and branch creation retain the source checkout and files.
        - The new branch starts at refreshed origin/main.
    """
    created, _ = api.install(path=git_project.root, settings=make_settings())
    assert created and api.install(path=git_project.root, settings=make_settings()) == (
        False,
        "",
    )
    git_project.commits.commit_changes(title="Configure")
    git_project.run_command("push", "origin", "main")
    head = git_project.commits.read_head_commit()
    assert api.fetch(path=git_project.root) == {"path": str(git_project.root)}
    assert api.create_branch(path=git_project.root, name="feature") == {
        "path": str(git_project.root),
        "branch": "feature",
    }
    assert git_project.branches.read_branch_head(name="feature") == head
    assert (
        git_project.branches.read_branch() == "main" and git_project.is_worktree_clean()
    )


def test_api_finish_and_release_cleanup(
    *, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: the API publishes a task, merges it, and removes selected resources.

    Invariants:
        - Finish returns the published PR number and exact feature revision.
        - Release tags the integration commit and updates the surviving main checkout.
        - Selected worktree and branches disappear; a retry reuses the same tag.
    """
    created = api.create_worktree(
        path=project.git.root, destination=project.workspace, branch="codex/task-17"
    )
    assert created == {"path": str(project.workspace), "branch": "codex/task-17"}
    task = Git(root=project.workspace)
    (task.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    published = api.finish(path=task.root, title="Feature")
    assert published["pr"]["number"] == 1
    assert published["pr"]["head_commit"] == task.commits.read_head_commit()
    selected = PullRequest(
        number=1,
        url="https://github.com/example/project/pull/1",
        head=task.branches.read_branch(),
        base="main",
        state="OPEN",
        commit="",
        head_commit=task.commits.read_head_commit(),
    )
    hosting = Mock(spec=GitHub)
    hosting.read_pull_request.side_effect = lambda **_: selected

    def merge_pull_request(*, number: int, head_commit: str) -> PullRequest:
        """Model the hosted merge by advancing only the isolated bare remote."""
        nonlocal selected
        assert number == selected.number and head_commit == selected.head_commit
        tree = task.run_command(
            "merge-tree", "--write-tree", "main", selected.head
        ).stdout.strip()
        commit = task.run_command(
            "commit-tree", tree, "-p", "main", "-p", head_commit, "-m", "Merge feature"
        ).stdout.strip()
        task.run_command("push", "origin", f"{commit}:refs/heads/main")
        selected = PullRequest.model_validate(
            {
                **selected.model_dump(),
                "state": "MERGED",
                "commit": commit,
            }
        )
        return selected

    hosting.merge_pull_request.side_effect = merge_pull_request
    monkeypatch.setattr(target="rail.commands.GitHub", name=Mock(return_value=hosting))
    result = api.release(
        path=task.root,
        pr=1,
        tag=True,
        delete_local_branch=True,
        delete_remote_branch=True,
        delete_worktree=True,
    )
    assert result["pr"]["commit"] == selected.commit and result["tag"] == "v1.2.3"
    assert result["deleted_worktree"] is True and not task.root.exists()
    assert (
        project.git.run_command("rev-parse", "main").stdout.strip() == selected.commit
    )
    assert (
        project.git.run_command("rev-parse", "v1.2.3^{commit}").stdout.strip()
        == selected.commit
    )
    assert project.git.branches.read_branch_head(name=selected.head) is None
    assert not project.git.run_command(
        "ls-remote", "--heads", "origin", f"refs/heads/{selected.head}"
    ).stdout
    assert project.git.is_worktree_clean()
    repeated = api.release(
        path=project.git.root,
        pr=1,
        tag=True,
        delete_local_branch=True,
        delete_remote_branch=True,
        delete_worktree=True,
    )
    assert repeated["tag"] == "v1.2.3" and hosting.merge_pull_request.call_count == 1


def test_api_check_defaults(
    *, git_project: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a caller omits the checkout path.

    Invariants:
        - The current directory is selected and its configured checks run.
    """
    write_settings(root=git_project.root, settings=make_settings())
    git_project.run_command(
        "remote", "set-url", "origin", "https://github.com/example/project"
    )
    monkeypatch.chdir(path=git_project.root)
    result = api.check()
    assert result["checks"] == [] and result["workspace"]["path"] == str(
        git_project.root
    )


@pytest.mark.parametrize(argnames="title", argvalues=[" ", "a\nb", "a\rb"])
def test_api_rejects_invalid_finish_title(
    *, title: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a library caller supplies a blank or multiline finish title.

    Invariants:
        - Invalid titles are rejected before repository access.
    """
    open_repository = Mock(side_effect=AssertionError("Unexpected repository access"))
    monkeypatch.setattr(target="rail.commands._open_repository", name=open_repository)
    with pytest.raises(expected_exception=ValidationError, match="title"):
        api.finish(title=title)
    open_repository.assert_not_called()


@pytest.mark.parametrize(argnames="pr", argvalues=[0, -1, True])
def test_api_rejects_invalid_pr_number(
    *, pr: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a library caller supplies an invalid repository-local PR number.

    Invariants:
        - Invalid numbers are rejected before repository access.
    """
    open_repository = Mock(side_effect=AssertionError("Unexpected repository access"))
    monkeypatch.setattr(target="rail.commands._open_repository", name=open_repository)
    with pytest.raises(expected_exception=ValidationError, match="number"):
        api.release(pr=pr)
    open_repository.assert_not_called()


def test_api_rejects_missing_repository(*, tmp_path: Path) -> None:
    """Context: mutation is requested outside Git.

    Invariants:
        - A structured repository error is raised without creating files.
    """
    with pytest.raises(expected_exception=OperationError) as error:
        api.fetch(path=tmp_path)
    assert error.value.code == "not_repository" and list(tmp_path.iterdir()) == []


def test_windows_worktree_deletion_requires_external_directory(
    *, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a Windows caller tries to remove its own process working directory.

    Invariants:
        - Release refuses the invalid invocation before merging or deleting anything.
    """
    task = create_task_checkout(project=project)
    monkeypatch.chdir(path=task.root)
    monkeypatch.setattr(target="rail.commands.os", name=SimpleNamespace(name="nt"))
    with pytest.raises(expected_exception=OperationError, match="outside the worktree"):
        api.release(pr=1, delete_worktree=True)
    assert task.is_worktree_clean()
