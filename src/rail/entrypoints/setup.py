"""Collect project configuration through interactive prompts."""

from __future__ import annotations

import sys

from rail.application.contracts import InstallationSettings, WorkflowError


def prompt_value(
    *, label: str, default: str, choices: tuple[str, ...] = (), blank: bool = False
) -> str:
    """Read one setup answer, using Enter to keep the displayed value.

    :param label: the setting's human-readable name.
    :param default: the current or initial setting value.
    :param choices: the permitted literal choices, empty for unrestricted text.
    :param blank: whether two double quotes explicitly select an empty value.

    :return: the selected value after required-content and choice validation.

    :raises WorkflowError: if input ends before setup is complete.
    """
    options = f" ({'/'.join(choices)})" if choices else ""
    hint = ' (use "" for no prefix)' if blank else ""
    displayed = default if default else '""'
    while True:
        print(
            f"[RAIL] {label}{options}{hint} [{displayed}]: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        answer = sys.stdin.readline()
        if not answer:
            raise WorkflowError("Setup input ended before all settings were collected.")
        value = answer.strip() or default
        if blank and value == '""':
            value = ""
        if not value and not blank:
            print("[RAIL] A nonblank value is required.", file=sys.stderr)
        elif choices and value not in choices:
            print(f"[RAIL] Choose {' or '.join(choices)}.", file=sys.stderr)
        else:
            return value


def confirm_setup(*, prompt: str) -> bool:
    """Read setup confirmation, accepting an empty answer as approval.

    :param prompt: the setup question displayed before the confirmation choices.

    :return: whether the user answered yes.

    :raises WorkflowError: if input closes before a choice is made.
    """
    while True:
        print(f"[RAIL] {prompt} [Y/n] ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline()
        if not answer:
            raise WorkflowError("Setup input ended before the choice was completed.")
        choice = answer.strip().casefold()
        if choice in {"", "y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print("[RAIL] Choose y or n.", file=sys.stderr)


def collect_installation_settings(
    *, current: InstallationSettings | None
) -> InstallationSettings:
    """Collect and validate all installation choices except template-defined checks.

    :param current: existing file settings for prompt hints, or None on first setup.

    :return: the complete validated installation choices.
    """
    remote = prompt_value(
        label="Git remote", default="" if current is None else current.git.remote
    )
    main = prompt_value(
        label="Main branch", default="" if current is None else current.git.main
    )
    policy = prompt_value(
        label="Version policy",
        default="" if current is None else current.release.policy,
        choices=("semver", "calver"),
    )
    prefix = prompt_value(
        label="Tag prefix",
        default="" if current is None else current.release.prefix,
        blank=True,
    )
    return InstallationSettings.model_validate(
        obj={
            "git": {"remote": remote, "main": main},
            "release": {"policy": policy, "prefix": prefix},
        }
    )
