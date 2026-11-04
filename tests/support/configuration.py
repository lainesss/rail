"""Build scenario settings from an explicit configuration fixture."""

from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rail.application.contracts import (
    CheckSettings,
    GitSettings,
    ReleaseSettings,
    Settings,
)


if TYPE_CHECKING:
    from rail.application.contracts import Check


def make_settings(
    *,
    remote: str | None = None,
    main: str | None = None,
    checks: tuple[Check, ...] | None = None,
    policy: Literal["semver", "calver"] | None = None,
    prefix: str | None = None,
) -> Settings:
    """Load complete fixture data and apply explicit scenario selections.

    :param remote: the selected remote, or None to retain the fixture value.
    :param main: the selected integration branch, or None to retain the fixture value.
    :param checks: the selected checks, or None to retain the fixture commands.
    :param policy: the selected version policy, or None to retain the fixture value.
    :param prefix: the selected tag prefix, or None to retain the fixture value.

    :return: the complete validated settings for the scenario.
    """
    document = Path(__file__).with_name("config.toml").read_text(encoding="utf-8")
    settings = Settings.model_validate(obj=tomllib.loads(document))
    return Settings(
        git=GitSettings(
            remote=settings.git.remote if remote is None else remote,
            main=settings.git.main if main is None else main,
        ),
        checks=settings.checks if checks is None else CheckSettings(commands=checks),
        release=ReleaseSettings(
            policy=settings.release.policy if policy is None else policy,
            prefix=settings.release.prefix if prefix is None else prefix,
        ),
    )


def write_settings(*, root: Path, settings: Settings) -> None:
    """Write a complete scenario file with explicitly selected check commands.

    :param root: the isolated repository receiving the scenario configuration.
    :param settings: the complete validated settings for the scenario.
    """
    commands = ",\n".join(
        "  { name = "
        + json.dumps(obj=command.name, ensure_ascii=False)
        + ", command = "
        + json.dumps(obj=shlex.join(split_command=command.args), ensure_ascii=False)
        + f", retries = {command.retries} }}"
        for command in settings.checks.commands
    )
    document = (
        "[git]\nremote = "
        + json.dumps(obj=settings.git.remote, ensure_ascii=False)
        + "\nmain = "
        + json.dumps(obj=settings.git.main, ensure_ascii=False)
        + "\n\n[checks]\ncommands = [\n"
        + commands
        + "\n]\n\n[release]\npolicy = "
        + json.dumps(obj=settings.release.policy, ensure_ascii=False)
        + "\nprefix = "
        + json.dumps(obj=settings.release.prefix, ensure_ascii=False)
        + "\n"
    ).replace("\x7f", "\\u007f")
    (root / "rail.toml").write_text(data=document, encoding="utf-8")
