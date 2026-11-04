"""Exercise argument parsing, adapter composition, diagnostics, and cleanup choices."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from filelock import FileLock, Timeout

from rail.adapters.configuration import read_settings
from rail.adapters.git.repository import Git, SourceState
from rail.adapters.github import GitHub
from rail.application.contracts import (
    Check,
    InstallationSettings,
    PullRequest,
    WorkflowError,
)
from rail.configuration import RuntimeConfig
from rail.entrypoints.cli import build_parser, run_cli
from tests.support.configuration import make_settings, write_settings


@pytest.fixture(name="composed")
def create_composed_adapters(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Mock:
    """Replace CLI adapters while preserving actual argument and output behavior.

    :param tmp_path: the isolated repository location.
    :param monkeypatch: the patch restoration facility.

    :return: the repository adapter double used by CLI composition.
    """
    monkeypatch.setattr(
        target="rail.entrypoints.commands.install.create_operation_lock",
        name=lambda **_: FileLock(lock_file=str(tmp_path / "operation.lock")),
    )
    repository = Mock(spec=Git(root=Path.cwd()))
    monkeypatch.setattr(
        target="rail.entrypoints.cli.sys.stdin",
        name=io.StringIO(initial_value="origin\nmain\nsemver\nv\n"),
    )
    repository.root = tmp_path
    repository.read_source_state.return_value = SourceState(
        branch="feature", head="a" * 40, dirty=False
    )
    repository.read_common_directory.return_value = tmp_path / ".git"
    repository.read_repository_root.return_value = tmp_path
    repository.remotes.read_remote_url.return_value = (
        "https://github.example/owner/project"
    )
    monkeypatch.setattr(
        target="rail.entrypoints.commands.install.Git",
        name=Mock(return_value=repository),
    )
    monkeypatch.setattr(
        target="rail.entrypoints.commands.install.GitCommandRunner",
        name=Mock(return_value=repository),
    )
    monkeypatch.setattr(target="rail.commands.Git", name=Mock(return_value=repository))
    monkeypatch.setattr(
        target="rail.commands.discover_git_root",
        name=lambda **_: repository.read_repository_root(),
    )
    monkeypatch.setattr(
        target="rail.commands.create_operation_lock",
        name=lambda **_: FileLock(lock_file=str(tmp_path / "operation.lock")),
    )
    monkeypatch.setattr(
        target="rail.commands.GitHub", name=Mock(return_value=Mock(spec=GitHub))
    )
    monkeypatch.setattr(
        target="rail.commands.read_settings", name=Mock(return_value=make_settings())
    )
    return repository


@pytest.mark.parametrize(
    argnames="arguments,expected",
    argvalues=[
        (
            ["--help"],
            (
                "install",
                "check",
                "finish",
                "release",
                "inspect",
                "fetch",
                "branch",
                "worktree",
            ),
        ),
        (["install", "--help"], ("--path",)),
        (["branch", "create", "--help"], ("--path", "--name", "--json")),
        (
            ["worktree", "create", "--help"],
            ("--path", "--destination", "--branch", "--json"),
        ),
        (["check", "--help"], ("--path", "--timeout", "--json", "default:")),
        (
            ["finish", "--help"],
            ("--title", "--body", "--body-file", "--timeout", "--json"),
        ),
        (
            ["release", "--help"],
            (
                "--pr",
                "--tag",
                "--delete-local-branch",
                "--delete-remote-branch",
                "--delete-worktree",
                "--json",
            ),
        ),
        (["inspect", "--help"], ("--path", "--json")),
        (["fetch", "--help"], ("--path", "--json")),
        (["--version"], ("rail ",)),
    ],
)
def test_help_and_version(
    *,
    arguments: list[str],
    expected: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Context: a user requests command help or the installed version.

    Invariants:
        - Parsing exits successfully and lists the expected commands or options.
        - No diagnostics are emitted to stderr.
    """
    with pytest.raises(expected_exception=SystemExit) as outcome:
        run_cli(argv=arguments)
    assert outcome.value.code == 0
    captured = capsys.readouterr()
    assert all(fragment in captured.out for fragment in expected)
    assert "[RAIL]" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    argnames="arguments",
    argvalues=[
        [],
        ["--unknown"],
        ["unknown"],
        ["branch", "create"],
        ["worktree", "create", "--branch", "feature"],
        ["worktree", "create", "--destination", "task"],
        ["finish"],
        ["finish", "--title", " "],
        ["finish", "--title", "first\nsecond"],
        ["finish", "--title", "Feature", "--body", "text", "--body-file", "body.txt"],
        ["release", "--pr", "zero"],
        ["release", "--pr", "0"],
        ["--log-format", "other", "check"],
        ["check", "--timeout", "0"],
        ["check", "--timeout", "invalid"],
    ],
)
def test_invalid_arguments(
    *, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Context: required command input is absent or invalid.

    Invariants:
        - Parsing exits with status two and one application-prefixed error.
    """
    with pytest.raises(expected_exception=SystemExit) as outcome:
        run_cli(argv=arguments)
    assert outcome.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("[RAIL] ERROR:") == 1


def test_version_without_metadata(
    *, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Context: the CLI runs without installed distribution metadata.

    Invariants:
        - The documented fallback version is displayed.
    """
    from importlib.metadata import PackageNotFoundError

    monkeypatch.setattr(
        target="rail.entrypoints.cli.version",
        name=Mock(side_effect=PackageNotFoundError),
    )
    with pytest.raises(expected_exception=SystemExit):
        build_parser().parse_args(args=["--version"])
    assert capsys.readouterr().out == "rail 0.0.0\n"


def test_repository_routes_before_discovery(*, monkeypatch: pytest.MonkeyPatch) -> None:
    """Context: a repository command receives its own directory through its arguments.

    Invariants:
        - Inspection receives the selected path without invoking installation.
    """
    boundary = Mock(return_value={"workspace": None})
    monkeypatch.setattr(
        target="rail.entrypoints.commands.inspect.inspect", name=boundary
    )
    repository = Mock(side_effect=AssertionError("unexpected discovery"))
    monkeypatch.setattr(target="rail.entrypoints.commands.install.Git", name=repository)
    assert run_cli(argv=["inspect"]) == 0
    boundary.assert_called_once_with(path=Path.cwd())
    repository.assert_not_called()


def test_interactive_operation_respects_shared_lock(
    *, composed: Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Context: another operation already holds the repository lock.

    Invariants:
        - Interactive commands reject concurrency before changing repository state.
    """
    assert composed.read_repository_root.return_value == tmp_path
    with FileLock(lock_file=str(tmp_path / "operation.lock")).acquire(timeout=0):
        assert run_cli(argv=["check"]) == 1
    composed.branches.validate_branch.assert_not_called()
    assert "check failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    argnames="created,difference",
    argvalues=[(True, ""), (False, "- old\n+ new\n"), (False, "")],
)
def test_install_output(
    *,
    composed: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    created: bool,
    difference: str,
) -> None:
    """Context: installation saves validated choices or updates existing configuration.

    Invariants:
        - Line-numbered diffs go to stdout while status messages go to stderr.
        - Created, updated, and unchanged configuration have distinct status messages.
    """
    root = composed.read_repository_root.return_value
    config = root / "rail.toml"
    choices = make_settings()
    selected = InstallationSettings(git=choices.git, release=choices.release)
    if difference:
        config.write_text(data="old\n", encoding="utf-8")
        stream = io.StringIO(initial_value="")
        monkeypatch.setattr(target=stream, name="isatty", value=lambda: True)
        monkeypatch.setattr(target="rail.entrypoints.cli.sys.stdin", name=stream)
        monkeypatch.setattr(
            target="rail.entrypoints.cli.sys.stderr.isatty", name=lambda: True
        )
        monkeypatch.setattr(
            target="rail.entrypoints.commands.install.confirm_setup",
            name=Mock(return_value=True),
        )
        monkeypatch.setattr(
            target="rail.entrypoints.commands.install.collect_installation_settings",
            name=Mock(return_value=selected),
        )
        monkeypatch.setattr(
            target="rail.entrypoints.commands.install.read_settings",
            name=Mock(return_value=make_settings()),
        )

    def save_configuration(**_: object) -> tuple[bool, str]:
        """Model saving the selected configuration before returning its diff."""
        if difference:
            config.write_text(data="new\n", encoding="utf-8")
        return created, difference

    installation = Mock(side_effect=save_configuration)
    monkeypatch.setattr(
        target="rail.entrypoints.commands.install.install_configuration",
        name=installation,
    )
    assert run_cli(argv=["install"]) == 0
    installation.assert_called_once_with(root=root, settings=selected)
    captured = capsys.readouterr()
    assert captured.out == ("1 - old\n1 + new\n" if difference else "")
    assert (
        "Configuration created"
        if created
        else "Configuration overwritten"
        if difference
        else "Configuration unchanged"
    ) in captured.err


@pytest.mark.parametrize(argnames="json_output", argvalues=[False, True])
def test_check_composition(
    *,
    composed: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    """Context: the CLI executes a configured project-owned script.

    Invariants:
        - Literal arguments execute from repository root with forwarded output.
        - The configured check name appears in diagnostics.
        - Console and JSON responses describe the completed check.
    """
    monkeypatch.setattr(
        target="rail.commands.read_settings",
        name=Mock(
            return_value=make_settings(
                checks=(
                    Check(
                        name="Project script", args=("python", "script.py"), retries=0
                    ),
                )
            )
        ),
    )
    execute = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(target="rail.adapters.checks.run_process", name=execute)
    arguments = ["check", "--timeout", "3600"]
    if json_output:
        arguments.append("--json")
    assert run_cli(argv=arguments) == 0
    execute.assert_called_once_with(
        args=("python", "script.py"),
        cwd=composed.read_repository_root.return_value,
        capture=False,
        forward_to_stderr=True,
        timeout=3600,
        check_status=False,
        project_environment=True,
    )
    captured = capsys.readouterr()
    assert "Project script" in captured.err
    if json_output:
        response = json.loads(s=captured.out)
        assert response["version"] == 1 and response["ok"] is True
        assert response["result"]["checks"] == [
            {
                "name": "Project script",
                "args": ["python", "script.py"],
                "exit_code": 0,
            }
        ]
        assert response["result"]["workspace"]["path"] == str(composed.root)
    else:
        assert captured.out == "[RAIL] Configured checks passed\n"


def test_real_named_scripts_use_individual_retries(
    *,
    git_project: Git,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Context: real configured scripts fail until their own retry counts are consumed.

    Invariants:
        - Commands with zero, one, and two retries run once, twice, and three times.
        - Successful previous commands are not repeated.
        - Names are displayed once per attempt, including names with spaces.
    """
    root = git_project.root
    script = root / "script with spaces.py"
    script.write_text(
        data=(
            "import pathlib,sys\n"
            "counter=pathlib.Path(sys.argv[1]+'.count')\n"
            "attempt=int(counter.read_text())+1 if counter.exists() else 1\n"
            "counter.write_text(str(attempt))\n"
            "raise SystemExit(attempt <= int(sys.argv[2]))\n"
        ),
        encoding="utf-8",
    )
    checks = tuple(
        Check(
            name=name,
            args=(sys.executable, script.name, name, str(failures)),
            retries=retries,
        )
        for name, failures, retries in (
            ("First check", 0, 0),
            ("Fixer", 2, 2),
            ("Last check", 1, 1),
        )
    )
    write_settings(root=root, settings=make_settings(checks=checks))
    monkeypatch.chdir(path=root)
    assert run_cli(argv=["check"]) == 0
    assert (root / "First check.count").read_text(encoding="utf-8") == "1"
    assert (root / "Fixer.count").read_text(encoding="utf-8") == "3"
    assert (root / "Last check.count").read_text(encoding="utf-8") == "2"
    diagnostics = capfd.readouterr().err
    assert diagnostics.count("First check") == 1
    assert diagnostics.count("Fixer") == 3
    assert diagnostics.count("Last check") == 2


@pytest.mark.parametrize(argnames="json_output", argvalues=[False, True])
def test_finish_composition(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    """Context: CLI finalization receives a title and multiline PR description.

    Invariants:
        - The description reaches the use case unchanged.
        - Console output is the PR URL; JSON preserves the publication result.
    """
    selected = PullRequest(
        number=1,
        url="https://github.example/owner/project/pull/1",
        head="feature",
        base="main",
        state="OPEN",
        commit="",
    )
    result = {
        "pr": selected.model_dump(),
        "checks": [],
        "workspace": {"path": str(tmp_path)},
    }
    operation = Mock(return_value=result)
    monkeypatch.setattr(
        target="rail.entrypoints.commands.finish.finish", name=operation
    )
    assert (
        run_cli(
            argv=[
                "finish",
                "--path",
                str(tmp_path),
                "--title",
                "Title",
                "--body",
                "first\nsecond",
                *(["--json"] if json_output else []),
            ]
        )
        == 0
    )
    operation.assert_called_once_with(
        path=tmp_path,
        title="Title",
        body="first\nsecond",
        timeout=RuntimeConfig.CHECK_TIMEOUT_SECONDS,
    )
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(s=output) == {"version": 1, "ok": True, "result": result}
    else:
        assert output == "https://github.example/owner/project/pull/1\n"


@pytest.mark.parametrize(
    argnames="flags",
    argvalues=[
        ("--tag",),
        ("--delete-local-branch",),
        ("--delete-remote-branch",),
        ("--delete-worktree",),
        (
            "--tag",
            "--delete-local-branch",
            "--delete-remote-branch",
            "--delete-worktree",
        ),
    ],
    ids=["tag", "local-branch", "remote-branch", "worktree", "all-actions"],
)
@pytest.mark.parametrize(argnames="json_output", argvalues=[False, True])
def test_release_cleanup(
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
    json_output: bool,
) -> None:
    """Context: release succeeds with explicit optional action flags.

    Invariants:
        - Console output reports the PR and requested tag; JSON preserves the result.
        - Each flag enables only its corresponding action without prompting.
    """
    selected = PullRequest(
        number=1,
        url="url",
        head="feature",
        base="main",
        state="MERGED",
        commit="a" * 40,
    )
    tag = "v1.2.3" if "--tag" in flags else None
    result = {"tag": tag, "pr": selected.model_dump()}
    operation = Mock(return_value=result)
    monkeypatch.setattr(
        target="rail.entrypoints.commands.release.release", name=operation
    )
    arguments = ["release", "--pr", "1", *flags]
    if json_output:
        arguments.append("--json")
    assert run_cli(argv=arguments) == 0
    operation.assert_called_once_with(
        path=Path.cwd(),
        pr=1,
        tag="--tag" in flags,
        delete_local_branch="--delete-local-branch" in flags,
        delete_remote_branch="--delete-remote-branch" in flags,
        delete_worktree="--delete-worktree" in flags,
    )
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(s=output) == {"version": 1, "ok": True, "result": result}
    else:
        assert output == "url\n" + (tag + "\n" if tag else "")


def test_release_defaults_do_not_tag_or_delete(
    *, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Context: release is invoked without any optional action flags.

    Invariants:
        - Release requests no tagging or deletion and prints only the PR.
    """
    selected = PullRequest(
        number=1,
        url="url",
        head="feature",
        base="main",
        state="MERGED",
        commit="a" * 40,
    )
    operation = Mock(return_value={"pr": selected.model_dump(), "tag": None})
    monkeypatch.setattr(
        target="rail.entrypoints.commands.release.release", name=operation
    )
    assert run_cli(argv=["release"]) == 0
    assert all(
        operation.call_args.kwargs[name] is False
        for name in (
            "tag",
            "delete_local_branch",
            "delete_remote_branch",
            "delete_worktree",
        )
    )
    assert capsys.readouterr().out == "url\n"


@pytest.mark.parametrize(
    argnames="error,status",
    argvalues=[
        (WorkflowError("failed"), 1),
        (OSError("disk error"), 1),
        (ValueError("invalid response"), 1),
        (KeyboardInterrupt(), 130),
    ],
)
def test_boundary_failures(
    *,
    composed: Mock,
    error: BaseException,
    status: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Context: repository discovery fails or the user interrupts execution.

    Invariants:
        - The boundary returns the documented failure or interruption status.
        - One outcome diagnostic appears on stderr.
    """
    composed.read_repository_root.side_effect = error
    assert run_cli(argv=["check"]) == status
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("[RAIL]") == 1


@pytest.mark.parametrize(argnames="source", argvalues=["file", "stdin"])
def test_finish_reads_utf8_description(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """Context: finish receives a UTF-8 PR description from a file or stdin.

    Invariants:
        - The decoded multiline description reaches publication unchanged.
    """
    payload = b"\xef\xbb\xbffirst\nsecond"
    description = tmp_path / "body.txt"
    description.write_bytes(data=payload)
    if source == "stdin":
        monkeypatch.setattr(
            target="rail.entrypoints.commands.finish.sys.stdin",
            name=io.TextIOWrapper(buffer=io.BytesIO(payload)),
        )
    selected = PullRequest(
        number=1,
        url="url",
        head="feature",
        base="main",
        state="OPEN",
        commit="",
    )
    operation = Mock(return_value={"pr": selected.model_dump()})
    monkeypatch.setattr(
        target="rail.entrypoints.commands.finish.finish", name=operation
    )
    assert (
        run_cli(
            argv=[
                "finish",
                "--title",
                "Feature",
                "--body-file",
                "-" if source == "stdin" else str(description),
            ]
        )
        == 0
    )
    assert operation.call_args.kwargs["body"] == "first\nsecond"


@pytest.mark.parametrize(argnames="payload", argvalues=[None, b"\xff"])
def test_finish_rejects_unreadable_description(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: bytes | None,
) -> None:
    """Context: the selected PR description file is missing or contains invalid UTF-8.

    Invariants:
        - File errors fail with one diagnostic before invoking publication.
        - An existing invalid file remains unchanged.
    """
    path = tmp_path / "description.txt"
    if payload is not None:
        path.write_bytes(data=payload)
    operation = Mock()
    monkeypatch.setattr(
        target="rail.entrypoints.commands.finish.finish", name=operation
    )
    assert run_cli(argv=["finish", "--title", "Feature", "--body-file", str(path)]) == 1
    operation.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("[RAIL]") == 1
    assert "finish failed:" in captured.err
    if payload is not None:
        assert path.read_bytes() == payload


def test_configuration_diff_keeps_line_numbers() -> None:
    """Context: configuration edits contain unchanged lines and character hints.

    Invariants:
        - Only changed numbered lines are rendered, with optional color.
    """
    from rail.entrypoints.commands.install import render_configuration_diff

    result = render_configuration_diff(
        before="keep\naaaaXaaaa\n", after="keep\naaaaYaaaa\n", color=True
    )
    assert "2 - aaaaXaaaa" in result and "2 + aaaaYaaaa" in result
    assert "keep" not in result and "? " not in result and "\x1b[" in result


def test_interactive_install_preserves_rejected_overwrite(
    *, composed: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a user declines overwriting configuration and chooses to ignore it.

    Invariants:
        - Existing configuration is preserved and the local ignore choice is saved.
    """
    root = composed.read_repository_root.return_value
    config = root / "rail.toml"
    config.write_text(data='[git]\nremote="upstream"\n', encoding="utf-8")
    stream = io.StringIO(initial_value="n\nn\n")
    monkeypatch.setattr(target=stream, name="isatty", value=lambda: True)
    monkeypatch.setattr(target="rail.entrypoints.cli.sys.stdin", name=stream)
    monkeypatch.setattr(
        target="rail.entrypoints.cli.sys.stderr.isatty", name=lambda: True
    )
    assert run_cli(argv=["install"]) == 0
    assert config.read_text(encoding="utf-8") == '[git]\nremote="upstream"\n'
    assert "/rail.toml" in (root / ".gitignore").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    argnames="error,status",
    argvalues=[
        (Timeout(lock_file="busy"), 1),
        (KeyboardInterrupt(), 130),
        (WorkflowError("invalid"), 1),
        (OSError("disk"), 1),
    ],
)
def test_install_boundary_failures(
    *, composed: Mock, error: BaseException, status: int
) -> None:
    """Context: installation discovery is busy, interrupted, or invalid.

    Invariants:
        - The CLI returns the documented failure or interruption status.
    """
    composed.read_repository_root.side_effect = error
    assert run_cli(argv=["install"]) == status


def test_install_with_redirected_output_collects_settings(
    *, composed: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: stdin reports a character device while stderr is redirected.

    Invariants:
        - Installation collects all four choices before rendering the configuration.
    """
    stream = io.StringIO(initial_value="origin\nmain\nsemver\nv\n")
    monkeypatch.setattr(target=stream, name="isatty", value=lambda: True)
    monkeypatch.setattr(target="rail.entrypoints.cli.sys.stdin", name=stream)
    monkeypatch.setattr(
        target="rail.entrypoints.cli.sys.stderr.isatty", name=lambda: False
    )
    assert run_cli(argv=["install"]) == 0
    assert stream.read() == ""
    assert (composed.read_repository_root.return_value / "rail.toml").exists()


@pytest.mark.parametrize(
    argnames="document", argvalues=["[", '[git]\nremote = "old"\n']
)
def test_install_repairs_invalid_configuration(
    *,
    composed: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    document: str,
) -> None:
    """Context: an existing configuration has broken TOML or missing required fields.

    Invariants:
        - Consenting to replacement warns about the invalid file and collects choices.
        - The saved configuration contains the chosen values and the Pre-commit check.
    """
    root = composed.root
    config = root / "rail.toml"
    config.write_text(data=document, encoding="utf-8")
    monkeypatch.setattr(
        target="rail.entrypoints.commands.install.sys.stdin",
        name=io.StringIO(initial_value="y\nupstream\ntrunk\ncalver\nr\n"),
    )
    assert run_cli(argv=["install"]) == 0
    assert read_settings(root=root) == make_settings(
        remote="upstream",
        main="trunk",
        policy="calver",
        prefix="r",
        checks=(
            Check(
                name="Pre-commit",
                args=("pre-commit", "run", "--all-files"),
                retries=1,
            ),
        ),
    )
    captured = capsys.readouterr()
    assert "Existing configuration is invalid" in captured.err
    assert "Configuration overwritten" in captured.err
    assert '+ remote = "upstream"' in captured.out
