"""Exercise the current CLI boundary and checkout inspection with real Git state."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from rail.adapters.git.command_runner import GitCommandRunner
from rail.adapters.git.repository import Git, discover_git_root
from rail.api import inspect
from rail.application.contracts import OperationError
from rail.entrypoints.cli import run_cli
from tests.support.configuration import make_settings, write_settings


if TYPE_CHECKING:
    from pathlib import Path


def test_empty_checks_report_no_execution(
    *, git_project: Git, capsys: pytest.CaptureFixture[str]
) -> None:
    """Context: the selected repository has no configured check commands.

    Invariants:
        - The CLI succeeds and explicitly reports that no checks ran.
    """
    write_settings(root=git_project.root, settings=make_settings())
    assert run_cli(argv=["check", "--path", str(git_project.root)]) == 0
    assert capsys.readouterr().out == "[RAIL] No checks were run\n"


def test_inspect_outside_git(
    *, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Context: inspection starts in an ordinary directory without a repository.

    Invariants:
        - The CLI succeeds with a null workspace and a single JSON response.
    """
    assert run_cli(argv=["inspect", "--path", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": 1,
        "ok": True,
        "result": {"workspace": None},
    }


def test_inspect_from_unrelated_process_directory(
    *, git_project: Git, tmp_path: Path
) -> None:
    """Context: an external process inspects an explicit checkout from another folder.

    Invariants:
        - Exactly one JSON result identifies the requested checkout.
        - Inspection reads local settings without requiring a hosted remote.
    """
    write_settings(root=git_project.root, settings=make_settings())
    git_project.run_command("remote", "remove", "origin")
    result = subprocess.run(
        args=[
            sys.executable,
            "-c",
            "from rail.entrypoints.cli import run_cli; raise SystemExit(run_cli())",
            "inspect",
            "--path",
            str(git_project.root),
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0 and result.stderr == ""
    workspace = json.loads(result.stdout)["result"]["workspace"]
    assert workspace["path"] == str(git_project.root)
    assert workspace["main"] == "main" and workspace["remote_url"] is None


def test_discovery_and_nested_inspection(*, git_project: Git, tmp_path: Path) -> None:
    """Context: discovery and inspection start from nested checkout directories.

    Invariants:
        - Nested and proposed directories resolve to the containing checkout.
        - Plain directories return no repository and file paths are rejected.
    """
    write_settings(root=git_project.root, settings=make_settings())
    assert discover_git_root(path=tmp_path / "not-created") is None
    nested = git_project.root / "nested"
    nested.mkdir()
    assert discover_git_root(path=nested / "not-created") == git_project.root
    assert inspect(path=nested)["workspace"]["path"] == str(git_project.root)
    with pytest.raises(expected_exception=OperationError, match="directory"):
        discover_git_root(path=git_project.root / "content.txt")


def test_inspection_of_unborn_and_detached_head(
    *, tmp_path: Path, git_project: Git
) -> None:
    """Context: checkouts have either an unborn branch or a detached commit.

    Invariants:
        - An unborn checkout has no head; a detached checkout has no branch.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    repository = Git(root=empty)
    repository.run_command("init", "--initial-branch=main")
    write_settings(root=empty, settings=make_settings())
    write_settings(root=git_project.root, settings=make_settings())
    unborn = inspect(path=empty)["workspace"]
    assert unborn["head"] is None and unborn["branch"] == "main"
    head = git_project.commits.read_head_commit()
    git_project.run_command("switch", "--detach")
    detached = inspect(path=git_project.root)["workspace"]
    assert detached["branch"] is None and detached["head"] == head


def test_discovery_rejects_inconsistent_git_root(
    *, git_project: Git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: Git reports a root that does not contain the requested directory.

    Invariants:
        - Discovery rejects the inconsistent root instead of returning it.
    """
    monkeypatch.setattr(
        target="rail.adapters.git.command_runner.GitCommandRunner.read_repository_root",
        name=lambda _: tmp_path / "foreign",
    )
    with pytest.raises(expected_exception=OperationError, match="contain"):
        discover_git_root(path=git_project.root)


@pytest.mark.parametrize(argnames="json_output", argvalues=[True, False])
def test_creation_and_fetch_cli(
    *, git_project: Git, capfd: pytest.CaptureFixture[str], json_output: bool
) -> None:
    """Context: the CLI creates a branch and task checkout, then fetches remote refs.

    Invariants:
        - Literal command arguments create the requested new objects.
        - Console and JSON modes report the actual resulting branch and path.
        - Fetch preserves the primary checkout.
    """
    extra = ["--json"] if json_output else []
    root = git_project.root
    head = git_project.commits.read_head_commit()
    write_settings(root=root, settings=make_settings())
    with (git_project.read_common_directory() / "info" / "exclude").open(
        mode="a", encoding="utf-8"
    ) as stream:
        stream.write("\n/rail.toml\n")
    assert (
        run_cli(
            argv=["branch", "create", "--name", "feature", "--path", str(root), *extra]
        )
        == 0
    )
    output = capfd.readouterr().out
    if json_output:
        assert json.loads(s=output) == {
            "version": 1,
            "ok": True,
            "result": {"path": str(root), "branch": "feature"},
        }
    else:
        assert output == "[RAIL] Branch created: feature\n"
    assert git_project.branches.read_branch_head(name="feature") == head
    destination = root.parent / "task"
    assert (
        run_cli(
            argv=[
                "worktree",
                "create",
                "--branch",
                "task",
                "--destination",
                str(destination),
                "--path",
                str(root),
                *extra,
            ]
        )
        == 0
    )
    output = capfd.readouterr().out
    if json_output:
        assert json.loads(s=output) == {
            "version": 1,
            "ok": True,
            "result": {"path": str(destination), "branch": "task"},
        }
    else:
        assert output == str(destination) + "\n"
    task = Git(root=destination)
    assert task.branches.read_branch() == "task"
    assert task.commits.read_head_commit() == head
    assert (destination / "content.txt").read_text(encoding="utf-8") == "base\n"
    assert run_cli(argv=["fetch", "--path", str(root), *extra]) == 0
    output = capfd.readouterr().out
    if json_output:
        assert json.loads(s=output) == {
            "version": 1,
            "ok": True,
            "result": {"path": str(root)},
        }
    else:
        assert output == f"[RAIL] Fetch completed for {root}\n"
    assert (
        git_project.branches.read_branch() == "main" and git_project.is_worktree_clean()
    )


@pytest.mark.parametrize(argnames="kind", argvalues=["branch", "worktree"])
@pytest.mark.parametrize(argnames="move_tracking_ref", argvalues=[False, True])
@pytest.mark.parametrize(argnames="configured", argvalues=[False, True])
def test_creation_uses_fresh_remote_main(
    *,
    git_project: Git,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    kind: str,
    move_tracking_ref: bool,
    configured: bool,
) -> None:
    """Context: another clone advances the integration branch while refs stay stale.
    Local settings optionally select a custom remote and integration branch.
    A command wrapper optionally rewinds the tracking ref after the real fetch.

    Invariants:
        - Creation starts at the fetched remote commit and includes its files.
        - Moving the tracking ref after fetch does not change the creation base.
        - The source branch, commit, and working files remain unchanged.
    """
    initial = git_project.commits.read_head_commit()
    remote, integration = (
        ("central", "integration/trunk") if configured else ("origin", "main")
    )
    if configured:
        git_project.run_command("remote", "rename", "origin", remote)
        git_project.run_command("push", remote, f"HEAD:refs/heads/{integration}")
    write_settings(
        root=git_project.root, settings=make_settings(remote=remote, main=integration)
    )
    with (git_project.read_common_directory() / "info" / "exclude").open(
        mode="a", encoding="utf-8"
    ) as stream:
        stream.write("\n/rail.toml\n")
    publisher = Git(root=tmp_path / "publisher")
    git_project.run_command(
        "clone",
        "--branch",
        integration,
        str(tmp_path / "origin.git"),
        str(publisher.root),
    )
    publisher.run_command("config", "user.name", "Test Developer")
    publisher.run_command("config", "user.email", "developer@example.test")
    (publisher.root / "latest-main.txt").write_text(
        data="latest main\n", encoding="utf-8", newline="\n"
    )
    publisher.commits.commit_changes(title="Advance remote main")
    latest = publisher.commits.read_head_commit()
    publisher.run_command("push", "origin", integration)
    assert latest != initial
    assert (
        git_project.remotes.read_remote_main_commit(main=integration, remote=remote)
        == initial
    )
    original_command = GitCommandRunner.run_command

    def run_command(
        self: GitCommandRunner,
        *args: str,
        allowed: tuple[int, ...] = (0,),
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run real Git commands and optionally move the tracking ref after fetch.

        :param args: the Git subcommand and arguments.
        :param allowed: the accepted exit statuses.
        :param capture: whether query output is captured.

        :return: the actual Git command result.
        """
        result = original_command(self, *args, allowed=allowed, capture=capture)
        if move_tracking_ref and args[0] == "fetch":
            original_command(
                self, "update-ref", f"refs/remotes/{remote}/{integration}", initial
            )
        return result

    monkeypatch.setattr(target=GitCommandRunner, name="run_command", value=run_command)
    destination = tmp_path / "task"
    arguments = (
        ["--name", "fresh-feature"]
        if kind == "branch"
        else ["--branch", "fresh-feature", "--destination", str(destination)]
    )
    assert (
        run_cli(
            argv=[kind, "create", "--path", str(git_project.root), *arguments, "--json"]
        )
        == 0
    )
    assert json.loads(s=capfd.readouterr().out)["ok"] is True
    assert git_project.branches.read_branch_head(name="fresh-feature") == latest
    assert (
        git_project.run_command("show", "fresh-feature:latest-main.txt").stdout
        == "latest main\n"
    )
    if kind == "worktree":
        assert Git(root=destination).commits.read_head_commit() == latest
        assert (destination / "latest-main.txt").read_text(encoding="utf-8") == (
            "latest main\n"
        )
    else:
        assert not destination.exists()
    assert (
        git_project.branches.read_branch() == "main"
        and git_project.commits.read_head_commit() == initial
    )
    assert (
        git_project.is_worktree_clean()
        and not (git_project.root / "latest-main.txt").exists()
    )


@pytest.mark.parametrize(argnames="kind", argvalues=["branch", "worktree"])
@pytest.mark.parametrize(argnames="configured", argvalues=[False, True])
def test_creation_requires_successful_remote_fetch(
    *,
    git_project: Git,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    kind: str,
    configured: bool,
) -> None:
    """Context: the selected remote is unavailable despite a cached integration ref.
    With local settings, origin/main remains available as an alternative base.

    Invariants:
        - Creation fails instead of using the cached commit.
        - No new branch or worktree is created and source state is preserved.
    """
    initial = git_project.commits.read_head_commit()
    if configured:
        git_project.run_command(
            "remote", "add", "central", str(tmp_path / "missing.git")
        )
        git_project.run_command(
            "update-ref", "refs/remotes/central/integration/trunk", initial
        )
    else:
        git_project.run_command(
            "remote", "set-url", "origin", str(tmp_path / "missing.git")
        )
    write_settings(
        root=git_project.root,
        settings=make_settings(
            remote="central" if configured else "origin",
            main="integration/trunk" if configured else "main",
        ),
    )
    with (git_project.read_common_directory() / "info" / "exclude").open(
        mode="a", encoding="utf-8"
    ) as stream:
        stream.write("\n/rail.toml\n")
    destination = tmp_path / "task"
    arguments = (
        ["--name", "fresh-feature"]
        if kind == "branch"
        else ["--branch", "fresh-feature", "--destination", str(destination)]
    )
    assert (
        run_cli(
            argv=[kind, "create", "--path", str(git_project.root), *arguments, "--json"]
        )
        == 1
    )
    assert json.loads(s=capfd.readouterr().out)["ok"] is False
    assert git_project.branches.read_branch_head(name="fresh-feature") is None
    assert not destination.exists()
    assert (
        git_project.branches.read_branch() == "main"
        and git_project.commits.read_head_commit() == initial
    )
    assert git_project.is_worktree_clean()


@pytest.mark.parametrize(argnames="in_git", argvalues=[True, False])
def test_console_inspection(
    *,
    git_project: Git,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    in_git: bool,
) -> None:
    """Context: a user requests normal console inspection inside or outside Git.

    Invariants:
        - Console output reports the local workspace or an explicit null workspace.
    """
    write_settings(root=git_project.root, settings=make_settings())
    assert (
        run_cli(
            argv=["inspect", "--path", str(git_project.root if in_git else tmp_path)]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert ("branch: main" in output) if in_git else (output == "workspace: null\n")


@pytest.mark.parametrize(
    argnames="error,category,status",
    argvalues=[
        (OperationError("wrong", code="invalid_request"), "invalid_request", 1),
        (OSError("disk"), "operation_failed", 1),
        (KeyboardInterrupt(), "interrupted", 130),
    ],
)
def test_json_failure_boundary(
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    category: str,
    status: int,
) -> None:
    """Context: a JSON command fails or is interrupted at its execution boundary.

    Invariants:
        - Exactly one failure response reports the error category and exit status.
    """
    monkeypatch.setattr(
        target="rail.entrypoints.commands.inspect.inspect",
        name=Mock(side_effect=error),
    )
    assert run_cli(argv=["inspect", "--json"]) == status
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and result["error"]["code"] == category
