"""Describe interactive setup choices and cancellation without real input."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest

from rail.adapters.configuration import install_configuration, read_settings
from rail.application.contracts import Check, InstallationSettings, WorkflowError
from rail.entrypoints.setup import (
    collect_installation_settings,
    confirm_setup,
    prompt_value,
)
from tests.support.configuration import make_settings


if TYPE_CHECKING:
    from pathlib import Path

    from rail.application.contracts import Settings


@pytest.mark.parametrize(
    argnames="answers,expected",
    argvalues=[
        (
            "\n" * 4,
            make_settings(
                checks=(
                    Check(
                        name="Pre-commit",
                        args=("pre-commit", "run", "--all-files"),
                        retries=1,
                    ),
                )
            ),
        ),
        (
            'upstream\ntrunk\ncalver\n""\n',
            make_settings(
                remote="upstream",
                main="trunk",
                checks=(
                    Check(
                        name="Pre-commit",
                        args=("pre-commit", "run", "--all-files"),
                        retries=1,
                    ),
                ),
                policy="calver",
                prefix="",
            ),
        ),
        (
            "\n\nother\ncalver\nr\n",
            make_settings(
                checks=(
                    Check(
                        name="Pre-commit",
                        args=("pre-commit", "run", "--all-files"),
                        retries=1,
                    ),
                ),
                policy="calver",
                prefix="r",
            ),
        ),
    ],
    ids=["accept-defaults", "custom-repository", "retry-invalid-policy"],
)
def test_questionnaire_choices(
    *,
    tmp_path: Path,
    answers: str,
    expected: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Context: setup receives empty, explicit, or invalid answers.

    Invariants:
        - Accepted answers produce the expected repository, checks, and release policy.
        - Invalid version policies are retried instead of becoming saved settings.
        - Setup does not ask for check names, commands, or retry counts.
        - Prompts show available version policies and leave stdout empty.
    """
    monkeypatch.setattr(
        target="rail.entrypoints.setup.sys.stdin", name=io.StringIO(answers)
    )
    install_configuration(root=tmp_path, settings=make_settings())
    assert collect_installation_settings(
        current=read_settings(root=tmp_path)
    ) == InstallationSettings(git=expected.git, release=expected.release)
    captured = capsys.readouterr()
    assert "Version policy (semver/calver) [semver]" in captured.err
    assert "Number of checks" not in captured.err
    assert "Check 1" not in captured.err
    assert captured.out == ""


def test_questionnaire_input_ends(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: input closes before all configuration answers have been collected.

    Invariants:
        - Setup raises a workflow rejection instead of returning partial settings.
    """
    monkeypatch.setattr(
        target="rail.entrypoints.setup.sys.stdin", name=io.StringIO("\n")
    )
    install_configuration(root=tmp_path, settings=make_settings())
    with pytest.raises(WorkflowError, match="Setup input ended"):
        collect_installation_settings(current=read_settings(root=tmp_path))


@pytest.mark.parametrize(
    argnames="answer,expected",
    argvalues=[("\n", True), ("y\n", True), ("no\n", False), ("invalid\nn\n", False)],
)
def test_confirmation_answers(
    *, monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool
) -> None:
    """Context: setup receives empty, explicit, or initially invalid confirmation.

    Invariants:
        - Only an accepted affirmative answer authorizes the action.
    """
    monkeypatch.setattr(
        target="rail.entrypoints.setup.sys.stdin", name=io.StringIO(answer)
    )
    assert confirm_setup(prompt="Overwrite?") is expected


def test_confirmation_end_and_required_value(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: confirmation input closes and a required value is initially blank.

    Invariants:
        - Closed input refuses confirmation; a blank required value is retried.
    """
    monkeypatch.setattr(target="rail.entrypoints.setup.sys.stdin", name=io.StringIO(""))
    with pytest.raises(expected_exception=WorkflowError, match="input ended"):
        confirm_setup(prompt="Overwrite?")
    monkeypatch.setattr(
        target="rail.entrypoints.setup.sys.stdin", name=io.StringIO("\norigin\n")
    )
    assert prompt_value(label="Remote", default="") == "origin"
