"""Exercise new worktree creation with isolated repositories."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from rail.adapters.git.command_runner import GitCommandRunner
from rail.adapters.git.repository import Git
from rail.application.contracts import OperationError, WorkflowError
from rail.application.workspaces import create_worktree
from tests.support.configuration import make_settings


if TYPE_CHECKING:
    import subprocess

    from tests.support.repository import Project


def test_create_preserves_source(*, project: Project) -> None:
    """Context: a new task worktree is created while the source has unfinished edits.

    Invariants:
        - Source branch, commit, and unfinished files remain unchanged.
        - Repeating creation fails instead of reusing the branch or destination.
    """
    repository = Git(root=project.git.root)
    before = repository.commits.read_head_commit()
    source = repository.root / "unfinished.txt"
    source.write_text(data="keep", encoding="utf-8")
    task = create_worktree(
        repository=repository,
        settings=make_settings(),
        path=project.workspace,
        branch="codex/task-17",
    )
    assert (
        repository.branches.read_branch() == "main"
        and repository.commits.read_head_commit() == before
    )
    assert source.read_text(encoding="utf-8") == "keep"
    assert (
        task.root == project.workspace
        and task.branches.read_branch() == "codex/task-17"
    )
    with pytest.raises(WorkflowError):
        create_worktree(
            repository=repository,
            settings=make_settings(),
            path=project.workspace,
            branch="codex/task-17",
        )
    assert source.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(argnames="obstacle", argvalues=["occupied", "registered"])
def test_failed_worktree_creation_can_be_retried(
    *, project: Project, obstacle: str
) -> None:
    """Context: an occupied directory or stale registration blocks a new checkout.

    Invariants:
        - Failure preserves existing files and registrations without creating a branch.
        - Clearing the obstruction allows retrying the same branch and destination.
    """
    repository = project.git
    destination = project.workspace
    preserved = destination.with_name(name="preserved")
    if obstacle == "occupied":
        destination.mkdir()
        (destination / "keep.txt").write_text(data="keep", encoding="utf-8")
    else:
        repository.run_command("worktree", "add", "--detach", str(destination), "HEAD")
        destination.rename(target=preserved)
    before = repository.worktrees.list_checkouts()
    with pytest.raises(expected_exception=WorkflowError):
        create_worktree(
            repository=repository,
            settings=make_settings(),
            path=destination,
            branch="new-task",
        )
    assert repository.branches.read_branch_head(name="new-task") is None
    assert repository.worktrees.list_checkouts() == before
    if obstacle == "occupied":
        assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"
        destination.rename(target=preserved)
    else:
        assert (preserved / "content.txt").read_text(encoding="utf-8") == "base\n"
        repository.run_command("worktree", "prune", "--expire", "now")
    task = create_worktree(
        repository=repository,
        settings=make_settings(),
        path=destination,
        branch="new-task",
    )
    assert (
        task.branches.read_branch() == "new-task"
        and task.commits.read_head_commit() == repository.commits.read_head_commit()
    )
    assert preserved.exists()


def test_existing_branch_survives_worktree_creation_failure(
    *, project: Project
) -> None:
    """Context: a new worktree requests a branch that already exists.

    Invariants:
        - The existing branch and checkout registrations remain unchanged.
        - No destination directory is created.
    """
    repository = project.git
    repository.run_command("branch", "existing", "HEAD")
    head = repository.branches.read_branch_head(name="existing")
    checkouts = repository.worktrees.list_checkouts()
    with pytest.raises(expected_exception=WorkflowError):
        create_worktree(
            repository=repository,
            settings=make_settings(),
            path=project.workspace,
            branch="existing",
        )
    assert repository.branches.read_branch_head(name="existing") == head
    assert repository.worktrees.list_checkouts() == checkouts
    assert not project.workspace.exists()


@pytest.mark.parametrize(argnames="dirty", argvalues=[False, True])
def test_failed_branch_attachment_preserves_unexpected_files(
    *, project: Project, monkeypatch: pytest.MonkeyPatch, dirty: bool
) -> None:
    """Context: a command wrapper rejects branch attachment after a real checkout.
    In one case, it also creates an unexpected file before reporting the failure.

    Invariants:
        - A clean detached checkout is removed and the same request can be retried.
        - Unexpected files prevent cleanup and both failures are reported.
        - No new branch remains after the failed attachment.
    """
    repository = project.git
    before = repository.worktrees.list_checkouts()
    original_command = GitCommandRunner.run_command

    def run_command(
        self: GitCommandRunner,
        *args: str,
        allowed: tuple[int, ...] = (0,),
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run real Git commands except for the injected branch-attachment failure.

        :param args: the Git subcommand and arguments.
        :param allowed: the accepted exit statuses.
        :param capture: whether query output is captured.

        :return: the actual Git command result.

        :raises WorkflowError: when the new checkout tries to attach its branch.
        """
        if self.root == project.workspace and args[0] == "switch":
            if dirty:
                (self.root / "keep.txt").write_text(data="keep", encoding="utf-8")
            raise WorkflowError("Branch attachment failed")
        return original_command(self, *args, allowed=allowed, capture=capture)

    with monkeypatch.context() as patch:
        patch.setattr(target=GitCommandRunner, name="run_command", value=run_command)
        with pytest.raises(
            expected_exception=WorkflowError, match="Branch attachment failed"
        ) as failure:
            create_worktree(
                repository=repository,
                settings=make_settings(),
                path=project.workspace,
                branch="new-task",
            )
    assert repository.branches.read_branch_head(name="new-task") is None
    if dirty:
        assert (project.workspace / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert "Could not remove the detached worktree" in str(failure.value)
        assert str(project.workspace) in str(failure.value)
        assert (project.workspace, "") in repository.worktrees.list_checkouts()
    else:
        assert not project.workspace.exists()
        assert repository.worktrees.list_checkouts() == before
        task = create_worktree(
            repository=repository,
            settings=make_settings(),
            path=project.workspace,
            branch="new-task",
        )
        assert (
            task.branches.read_branch() == "new-task"
            and task.commits.read_head_commit() == repository.commits.read_head_commit()
        )


@pytest.mark.parametrize(
    argnames="destination,branch",
    argvalues=[("nested", "feature"), ("metadata", "feature"), ("outside", "main")],
)
def test_worktree_creation_rejects_unsafe_request(
    *, project: Project, destination: str, branch: str
) -> None:
    """Context: a worktree is requested inside source/metadata or on a reserved branch.

    Invariants:
        - Invalid requests preserve the source checkout and do not create a task.
    """
    repository = Git(root=project.git.root)
    path = {
        "nested": repository.root / "nested",
        "metadata": repository.read_common_directory() / "nested",
        "outside": project.workspace,
    }[destination]
    with pytest.raises(expected_exception=WorkflowError):
        create_worktree(
            repository=repository, settings=make_settings(), path=path, branch=branch
        )
    assert (
        repository.branches.read_branch() == "main" and not project.workspace.exists()
    )


@pytest.mark.parametrize(argnames="reserved", argvalues=[False, True])
def test_worktree_uses_configured_base(*, tmp_path: Path, reserved: bool) -> None:
    """Context: worktree rules receive custom settings and a substituted Git boundary.

    Invariants:
        - A task branch uses the configured remote and integration branch.
        - The configured integration name is rejected before worktree creation.
    """
    repository = Mock(spec=Git(root=Path.cwd()))
    repository.root = tmp_path / "source"
    repository.read_common_directory.return_value = repository.root / ".git"
    repository.branches.read_branch_head.return_value = None
    destination = tmp_path / "task"
    repository.open_checkout.return_value = Mock(
        spec=Git(root=destination), root=destination
    )
    settings = make_settings(remote="central", main="integration/trunk")
    if reserved:
        with pytest.raises(expected_exception=OperationError) as failure:
            create_worktree(
                repository=repository,
                settings=settings,
                path=destination,
                branch="integration/trunk",
            )
        assert failure.value.code == "invalid_request"
        repository.worktrees.add_worktree.assert_not_called()
        repository.remotes.fetch_main_commit.assert_not_called()
    else:
        task = create_worktree(
            repository=repository, settings=settings, path=destination, branch="feature"
        )
        assert task.root == destination
        repository.remotes.fetch_main_commit.assert_called_once_with(
            main="integration/trunk", remote="central"
        )
        repository.worktrees.add_worktree.assert_called_once_with(
            path=destination,
            branch="feature",
            commit=repository.remotes.fetch_main_commit.return_value,
        )
