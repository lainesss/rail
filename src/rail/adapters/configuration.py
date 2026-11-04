"""Render installation choices and read validated project-local TOML settings."""

from __future__ import annotations

import difflib
import tomllib
from importlib.resources import files
from typing import TYPE_CHECKING

from jinja2 import Environment, StrictUndefined

from rail.application.contracts import Settings, WorkflowError


if TYPE_CHECKING:
    from pathlib import Path

    from rail.application.contracts import InstallationSettings


def read_settings(*, root: Path) -> Settings:
    """Read and validate the project's configuration.

    :param root: the repository root containing rail.toml.

    :return: the validated workflow settings.
    """
    path = root / "rail.toml"
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return Settings.model_validate(obj=document)


def render_configuration(*, settings: InstallationSettings) -> str:
    """Render validated choices using the packaged configuration template.

    :param settings: the complete validated project settings to serialize.

    :return: the complete TOML configuration.
    """
    template = files(anchor="rail.resources").joinpath("config.toml.j2")
    environment = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.policies["json.dumps_kwargs"] = {
        "sort_keys": True,
        "ensure_ascii": False,
    }
    return (
        environment.from_string(source=template.read_text(encoding="utf-8"))
        .render(settings=settings)
        .replace("\x7f", "\\u007f")
    )


def install_configuration(
    *, root: Path, settings: InstallationSettings
) -> tuple[bool, str]:
    """Render complete installation choices into the project's configuration file.

    :param root: the repository root to initialize.
    :param settings: the complete validated repository and release choices.

    :return: whether configuration was created and the actual saved line changes,
        empty when the contents are unchanged.

    :raises WorkflowError: if configuration paths are symbolic links.
    """
    config = root / "rail.toml"
    if config.is_symlink():
        raise WorkflowError("Configuration paths must not be symbolic links.")
    created = not config.exists()
    current = "" if created else config.read_text(encoding="utf-8")
    document = render_configuration(settings=settings)
    config.write_text(data=document, encoding="utf-8", newline="\n")
    difference = "".join(
        line if line.endswith("\n") else line + "\n"
        for line in difflib.ndiff(
            a=current.splitlines(keepends=True),
            b=document.splitlines(keepends=True),
        )
        if line.startswith(("- ", "+ "))
    )
    return created, difference


def add_configuration_ignore_rule(*, root: Path) -> None:
    """Append a configuration-only ignore rule without changing existing entries.

    :param root: the repository whose ignore file is updated.

    :raises WorkflowError: if the ignore file is a symbolic link.
    """
    path = root / ".gitignore"
    if path.is_symlink():
        raise WorkflowError("The .gitignore file must not be a symbolic link.")
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    rule = "/rail.toml"
    if rule in current.splitlines():
        return
    block = f"# Workflow configuration\n{rule}\n"
    separator = ""
    if current and not current.endswith("\n\n"):
        separator = "\n" if current.endswith("\n") else "\n\n"
    with path.open(mode="a", encoding="utf-8", newline="\n") as stream:
        stream.write(separator + block)
