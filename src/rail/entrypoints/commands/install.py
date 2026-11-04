"""Register interactive configuration installation and render its saved changes."""

from __future__ import annotations

import difflib
import logging
import os
import sys
from typing import TYPE_CHECKING

from rail.adapters.configuration import (
    add_configuration_ignore_rule,
    install_configuration,
    read_settings,
)
from rail.adapters.git.command_runner import GitCommandRunner
from rail.adapters.git.repository import Git
from rail.adapters.locking import create_operation_lock
from rail.configuration import RuntimeConfig
from rail.entrypoints.arguments import (
    CheckoutArguments,
    add_path_option,
    validate_command_arguments,
)
from rail.entrypoints.setup import collect_installation_settings, confirm_setup


if TYPE_CHECKING:
    import argparse

    from rail.entrypoints.arguments import CommandNamespace


logger = logging.getLogger(name="workflow")


def register_install_command[Parser: argparse.ArgumentParser](
    *, commands: argparse._SubParsersAction[Parser]
) -> None:
    """Register interactive configuration installation and its checkout selection.

    :param commands: the root parser's command collection.
    """
    parser = commands.add_parser(
        "install", help="collect project settings and render rail.toml"
    )
    add_path_option(parser=parser)
    parser.set_defaults(
        handler=run_install_command, command_parser=parser, json_output=False
    )


def run_install_command(namespace: CommandNamespace) -> None:
    """Collect installation choices, save the configuration, and display changes.

    :param namespace: the selected installation command's parsed arguments.
    """
    arguments = validate_command_arguments(namespace=namespace, model=CheckoutArguments)
    root = GitCommandRunner(
        root=arguments.path.expanduser().resolve()
    ).read_repository_root()
    repository = Git(root=root)
    with create_operation_lock(repository=repository).acquire(
        timeout=RuntimeConfig.LOCK_TIMEOUT_SECONDS
    ):
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
        config = root / "rail.toml"
        existing = config.exists()
        previous = config.read_text(encoding="utf-8") if existing else ""
        replacing = False
        if existing and not confirm_setup(prompt="Overwrite existing configuration?"):
            created, changed = False, False
        else:
            replacing = existing
            current = None
            if existing:
                try:
                    current = read_settings(root=root)
                except (OSError, ValueError) as error:
                    logger.warning("Existing configuration is invalid: %s", error)
            selected = collect_installation_settings(current=current)
            created, difference = install_configuration(root=root, settings=selected)
            changed = bool(difference)
        ignored = interactive and not confirm_setup(prompt="Track rail.toml in Git?")
        if ignored:
            add_configuration_ignore_rule(root=root)
        logger.info(
            msg=(
                "Configuration created"
                if created
                else (
                    "Configuration overwritten"
                    if replacing
                    else "Configuration unchanged"
                )
            )
        )
        if changed:
            print(
                render_configuration_diff(
                    before=previous,
                    after=config.read_text(encoding="utf-8"),
                    color=sys.stdout.isatty() and "NO_COLOR" not in os.environ,
                ),
                end="",
            )
        if ignored:
            logger.info(msg="Configuration added to .gitignore")


def render_configuration_diff(*, before: str, after: str, color: bool) -> str:
    """Render configuration changes with their source line numbers.

    :param before: the configuration contents before the write.
    :param after: the configuration contents after the write.
    :param color: whether the output terminal permits ANSI color sequences.

    :return: removed and added lines with their corresponding line numbers.
    """
    shades = {"- ": "\x1b[38;2;255;123;114m", "+ ": "\x1b[38;2;126;231;135m"}
    rendered: list[str] = []
    old_line = 1
    new_line = 1
    for line in difflib.ndiff(a=before.splitlines(), b=after.splitlines()):
        marker = line[:2]
        content = line[2:]
        if marker == "  ":
            old_line += 1
            new_line += 1
            continue
        if marker == "? ":
            continue
        if marker == "- ":
            number = old_line
            old_line += 1
        else:
            number = new_line
            new_line += 1
        text = f"{number} {marker}{content}\n"
        shade = shades[marker]
        rendered.append(
            text if not color else f"{shade}{text.removesuffix(chr(10))}\x1b[0m\n"
        )
    return "".join(rendered)
