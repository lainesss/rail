"""Isolate diagnostic state and create disposable Git repositories."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import pytest
import structlog

from rail.adapters.git.repository import Git
from rail.adapters.github import GitHub
from rail.application.contracts import PullRequest
from tests.support.configuration import make_settings, write_settings
from tests.support.repository import Project


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def restore_logging_configuration() -> Iterator[None]:
    """Restore logging state changed by CLI and diagnostic tests.

    :return: control while a test owns its temporary diagnostic configuration.
    """
    logger = logging.getLogger(name="workflow")
    handlers = logger.handlers.copy()
    level = logger.level
    propagate = logger.propagate
    configuration = structlog.get_config()
    configured = structlog.is_configured()
    yield
    for handler in logger.handlers:
        if handler not in handlers:
            handler.close()
    logger.handlers = handlers
    logger.setLevel(level=level)
    logger.propagate = propagate
    structlog.reset_defaults()
    if configured:
        structlog.configure(**configuration)


@pytest.fixture(name="git_project")
def create_git_project(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Git:
    """Create an initialized repository and a local bare origin.

    :param tmp_path: the isolated test directory.
    :param monkeypatch: the environment restoration facility.

    :return: the working repository adapter with a published initial main.
    """
    monkeypatch.setenv(name="GIT_CONFIG_GLOBAL", value=os.devnull)
    monkeypatch.setenv(name="GIT_CONFIG_NOSYSTEM", value="1")
    root = tmp_path / "project"
    root.mkdir()
    git = Git(root=root)
    git.run_command("init", "--initial-branch=main")
    git.run_command("config", "user.name", "Test Developer")
    git.run_command("config", "user.email", "developer@example.test")
    git.run_command("config", "commit.gpgsign", "false")
    git.run_command("config", "core.autocrlf", "false")
    (root / "pyproject.toml").write_text(
        data='[project]\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (root / "content.txt").write_text(data="base\n", encoding="utf-8")
    git.commits.commit_changes(title="Initial")
    bare = tmp_path / "origin.git"
    git.run_command("init", "--bare", str(bare))
    git.run_command("remote", "add", "origin", str(bare))
    git.run_command("push", "--set-upstream", "origin", "main")
    return git


@pytest.fixture(name="project")
def create_project(*, git_project: Git, monkeypatch: pytest.MonkeyPatch) -> Project:
    """Install tracked workflow configuration and substitute only hosted PR effects.

    :param git_project: the real source and bare origin fixture.
    :param monkeypatch: the hosting boundary restorer.

    :return: the configured repository with independent task workspace storage.
    """
    write_settings(root=git_project.root, settings=make_settings())
    (git_project.root / ".gitignore").write_text(data=".attempt\n", encoding="utf-8")
    git_project.commits.commit_changes(title="Configure workflow")
    git_project.run_command("push", "origin", "main")
    control = Project(
        git=git_project, workspace=git_project.root.parent / "task-workspace"
    )

    class LocalGitHub(GitHub):
        """Replace hosted GitHub writes while observing real local feature commits.

        :param root: the checkout used for hosted operations.
        :param url: the configured remote, replaced only at this service boundary.
        """

        def __init__(self, *, root: Path, url: str) -> None:
            self.root = root
            self.repository = "github.com/example/project"
            assert url

        def find_pull_requests(
            self, *, branch: str, main: str, state: str
        ) -> list[PullRequest]:
            """Return the recorded PR only after its controlled creation.

            :param branch: the exact feature branch.
            :param main: the integration branch.
            :param state: the requested hosting state.

            :return: the existing PR, or an empty list before creation.
            """
            assert branch == "codex/task-17" and main == "main" and state == "open"
            return [self.read_pull_request(number=1)] if control.created else []

        def read_pull_request(
            self, *, number: int, expected_head: str | None = None
        ) -> PullRequest:
            """Read the actual feature commit from the isolated local repository.

            :param number: the selected test PR number.
            :param expected_head: the published commit, or None for an ordinary read.

            :return: the current feature revision with the controlled hosted URL.
            """
            assert number == 1
            head = (
                Git(root=self.root)
                .run_command("rev-parse", "refs/heads/codex/task-17")
                .stdout.strip()
            )
            assert expected_head is None or expected_head == head
            return PullRequest(
                number=1,
                url="https://github.com/example/project/pull/1",
                head="codex/task-17",
                base="main",
                state="OPEN",
                commit="",
                head_commit=head,
            )

        def create_pull_request(
            self, *, branch: str, main: str, title: str, body: str
        ) -> PullRequest:
            """Record one hosted publication without sending network traffic.

            :param branch: the published feature.
            :param main: the integration branch.
            :param title: the PR title.
            :param body: the PR description.

            :return: the created PR and its actual local feature commit.
            """
            assert (
                branch == "codex/task-17" and main == "main" and isinstance(body, str)
            )
            control.created.append(title)
            return self.read_pull_request(number=1)

    monkeypatch.setattr(target="rail.commands.GitHub", name=LocalGitHub)
    return control
