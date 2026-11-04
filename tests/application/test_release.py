"""Exercise release preflight against real local repositories and controlled hosting."""

from __future__ import annotations

import shutil
import tomllib
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from rail.adapters.git.command_runner import GitCommandRunner
from rail.adapters.github import GitHub
from rail.api import release
from rail.application.contracts import PullRequest, WorkflowError
from tests.support.repository import create_task_checkout


if TYPE_CHECKING:
    import subprocess

    from rail.adapters.git.repository import Git
    from tests.support.repository import Project


@pytest.fixture(name="release_case")
def create_release_case(
    *, project: Project, monkeypatch: pytest.MonkeyPatch
) -> tuple[Git, Mock]:
    """Prepare a clean feature and replace hosted merging with a local remote update.

    :param project: the source repository and bare local remote.
    :param monkeypatch: the boundary restoration facility.

    :return: the feature checkout and its controlled hosting adapter.
    """
    task = create_task_checkout(project=project)
    (task.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    task.commits.commit_changes(title="Feature")
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
    hosting.read_pull_request.return_value = selected

    def merge_pull_request(*, number: int, head_commit: str) -> PullRequest:
        """Model GitHub's squash merge in the temporary bare remote.

        :param number: the expected PR number.
        :param head_commit: the exact feature commit to merge.

        :return: the merged PR with its actual local integration commit.
        """
        assert number == 1 and head_commit == task.commits.read_head_commit()
        main = task.remotes.read_remote_main_commit(main="main", remote="origin")
        tree = task.run_command(
            "merge-tree", "--write-tree", main, head_commit
        ).stdout.strip()
        commit = task.run_command(
            "commit-tree", tree, "-p", main, "-m", "Squash feature"
        ).stdout.strip()
        task.run_command("push", "origin", f"{commit}:refs/heads/main")
        merged = PullRequest.model_validate(
            {
                **selected.model_dump(),
                "state": "MERGED",
                "commit": commit,
                "head_commit": head_commit,
            }
        )
        hosting.read_pull_request.return_value = merged
        return merged

    hosting.merge_pull_request.side_effect = merge_pull_request
    monkeypatch.setattr(target="rail.commands.GitHub", name=Mock(return_value=hosting))
    return task, hosting


@pytest.mark.parametrize(
    argnames="problem,diagnostic",
    argvalues=[
        ("missing-version", "project.version"),
        ("invalid-version", "project.version"),
        ("invalid-tag", "check-ref-format"),
        ("occupied-tag", "Release tag points"),
        ("divergent-main", "unpublished or divergent"),
        ("conflict", "conflicts with refreshed main"),
    ],
)
def test_release_preflight_blocks_external_actions(
    *,
    project: Project,
    release_case: tuple[Git, Mock],
    problem: str,
    diagnostic: str,
) -> None:
    """Context: release encounters invalid metadata, tags, local main, or merge content.

    Invariants:
        - The specific problem is reported before GitHub merge or remote cleanup.
        - The feature checkout, branch heads, and remote main remain unchanged.
    """
    task, hosting = release_case
    cleanup = problem == "divergent-main"
    if problem in {"missing-version", "invalid-version"}:
        document = (
            '[project]\nname = "fixture"\n'
            if problem == "missing-version"
            else '[project]\nversion = "not-semver"\n'
        )
        (task.root / "pyproject.toml").write_text(data=document, encoding="utf-8")
        task.commits.commit_changes(title="Invalid release metadata")
    elif problem == "invalid-tag":
        config = task.root / "rail.toml"
        config.write_text(
            data=config.read_text(encoding="utf-8").replace(
                'prefix = "v"', 'prefix = "bad prefix"'
            ),
            encoding="utf-8",
        )
        task.commits.commit_changes(title="Invalid tag prefix")
    elif problem == "occupied-tag":
        project.git.run_command("tag", "v1.2.3", "main")
    elif problem == "divergent-main":
        (project.git.root / "unpublished.txt").write_text(
            data="local work", encoding="utf-8"
        )
        project.git.commits.commit_changes(title="Unpublished local main")
    else:
        (project.git.root / "content.txt").write_text(
            data="main change", encoding="utf-8"
        )
        project.git.commits.commit_changes(title="Advance main")
        project.git.run_command("push", "origin", "main")
        (task.root / "content.txt").write_text(data="feature change", encoding="utf-8")
        task.commits.commit_changes(title="Conflicting feature")
    task.run_command("push", "origin", f"HEAD:refs/heads/{task.branches.read_branch()}")
    hosting.read_pull_request.return_value = PullRequest.model_validate(
        {
            **hosting.read_pull_request.return_value.model_dump(),
            "head_commit": task.commits.read_head_commit(),
        }
    )
    before_feature = task.commits.read_head_commit()
    before_main = project.git.commits.read_head_commit()
    before_remote = task.run_command(
        "ls-remote", "--heads", "origin", "refs/heads/main"
    ).stdout
    expected_error = (
        ValidationError
        if problem in {"missing-version", "invalid-version"}
        else WorkflowError
    )
    with pytest.raises(expected_exception=expected_error, match=diagnostic):
        release(
            path=task.root,
            pr=1,
            tag=True,
            delete_remote_branch=True,
            delete_local_branch=cleanup,
            delete_worktree=cleanup,
        )
    hosting.merge_pull_request.assert_not_called()
    assert task.root.exists() and task.commits.read_head_commit() == before_feature
    assert project.git.commits.read_head_commit() == before_main
    assert (
        task.run_command("ls-remote", "--heads", "origin", "refs/heads/main").stdout
        == before_remote
    )


def test_preflight_reads_version_from_prospective_merge(
    *, project: Project, release_case: tuple[Git, Mock]
) -> None:
    """Context: remote main changes the version after the feature branch was created.

    Invariants:
        - Tag preflight and publication use the merged version rather than stale HEAD.
        - The successful tag targets the actual integration commit.
    """
    task, hosting = release_case
    (project.git.root / "pyproject.toml").write_text(
        data='[project]\nversion = "2.0.0"\n', encoding="utf-8"
    )
    project.git.commits.commit_changes(title="Advance version on main")
    project.git.run_command("push", "origin", "main")
    task.run_command("push", "origin", f"HEAD:refs/heads/{task.branches.read_branch()}")
    document = task.commits.read_file(commit="HEAD", path="pyproject.toml")
    assert tomllib.loads(document)["project"]["version"] == "1.2.3"
    result = release(path=task.root, pr=1, tag=True)
    integrated = str(result["pr"]["commit"])
    parents = task.run_command(
        "rev-list", "--parents", "--max-count=1", integrated
    ).stdout.split()
    assert parents == [integrated, project.git.commits.read_head_commit()]
    assert not task.commits.contains_commit(
        commit=task.commits.read_head_commit(), main_commit=integrated
    )
    assert result["tag"] == "v2.0.0"
    assert (
        task.run_command("rev-parse", "v2.0.0^{commit}").stdout.strip()
        == result["pr"]["commit"]
    )
    hosting.merge_pull_request.assert_called_once()


@pytest.mark.parametrize(argnames="stage", argvalues=["remote", "worktree", "local"])
@pytest.mark.parametrize(argnames="after", argvalues=[False, True])
def test_cleanup_retry_uses_actual_git_state(
    *,
    project: Project,
    release_case: tuple[Git, Mock],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    after: bool,
) -> None:
    """Context: Git fails before or after a cleanup effect; hosting is controlled.

    Invariants:
        - Independent local cleanup proceeds despite remote deletion failure.
        - Retry finishes from the primary checkout without another merge or tag.
        - No local recovery records are created.
    """
    task, hosting = release_case
    task.run_command("push", "origin", f"HEAD:refs/heads/{task.branches.read_branch()}")
    original = GitCommandRunner.run_command
    failed = False

    def run_command(
        self: GitCommandRunner,
        *args: str,
        allowed: tuple[int, ...] = (0,),
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Inject one failure around an actual Git cleanup command.

        :param args: the Git command arguments.
        :param allowed: accepted exit statuses.
        :param capture: whether output is captured.

        :return: the actual Git result when no failure is injected.

        :raises WorkflowError: once at the selected cleanup boundary.
        """
        nonlocal failed
        hit = (
            (
                stage == "remote"
                and args[0] == "push"
                and args[-1].startswith(":refs/heads/")
            )
            or (stage == "worktree" and args[:2] == ("worktree", "remove"))
            or (stage == "local" and args[:3] == ("branch", "--delete", "--force"))
        )
        if hit and not failed:
            failed = True
            if after:
                original(self, *args, allowed=allowed, capture=capture)
            raise WorkflowError("Injected cleanup failure")
        return original(self, *args, allowed=allowed, capture=capture)

    monkeypatch.setattr(target=GitCommandRunner, name="run_command", value=run_command)
    options = {
        "path": project.git.root,
        "pr": 1,
        "tag": True,
        "delete_local_branch": True,
        "delete_remote_branch": True,
        "delete_worktree": True,
    }
    with pytest.raises(
        expected_exception=WorkflowError, match="Injected cleanup failure"
    ):
        release(**options)
    tag_object = project.git.run_command("rev-parse", "v1.2.3").stdout.strip()
    if stage == "remote":
        assert not task.root.exists()
    result = release(**options)
    assert result["tag"] == "v1.2.3" and not task.root.exists()
    assert project.git.branches.read_branch_head(name="codex/task-17") is None
    assert project.git.run_command("rev-parse", "v1.2.3").stdout.strip() == tag_object
    assert (
        project.git.run_command("rev-parse", "v1.2.3^{commit}").stdout.strip()
        == (result["pr"]["commit"])
    )
    assert project.git.commits.read_head_commit() == result["pr"]["commit"]
    assert not project.git.run_command(
        "ls-remote", "--heads", "origin", "refs/heads/codex/task-17"
    ).stdout
    assert not (project.git.read_common_directory() / "releases").exists()
    hosting.merge_pull_request.assert_called_once()


def test_retry_cleans_missing_worktree_registration(
    *, project: Project, release_case: tuple[Git, Mock]
) -> None:
    """Context: a published release's directory disappears but remains registered.

    Invariants:
        - Retry removes the stale registration and local branch without another merge.
    """
    task, hosting = release_case
    release(path=task.root, pr=1)
    assert task.root.resolve().is_relative_to(project.git.root.parent.resolve())
    shutil.rmtree(path=task.root)
    result = release(
        path=project.git.root, pr=1, delete_worktree=True, delete_local_branch=True
    )
    assert result["deleted_worktree"] is True
    assert not any(
        path == task.root for path, _ in project.git.worktrees.list_checkouts()
    )
    assert project.git.branches.read_branch_head(name="codex/task-17") is None
    hosting.merge_pull_request.assert_called_once()
