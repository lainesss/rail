"""Read tag targets and resume tag publication without moving existing tags."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import WorkflowError


if TYPE_CHECKING:
    from rail.adapters.git.command_runner import GitCommandRunner


class Tags:
    """Manage local and remote release tag references.

    :param command_runner: the Git subprocess runner for this checkout.
    """

    def __init__(self, *, command_runner: GitCommandRunner) -> None:
        self._command_runner = command_runner

    def read_tags(self, *, remote: str) -> dict[str, str]:
        """Combine local and remote tag targets, peeling annotated tags.

        :param remote: the configured authority.

        :return: tag names and targets; remote targets take precedence.
        """
        local = self._command_runner.run_command(
            "for-each-ref",
            "--format=%(refname:strip=2) %(objectname) %(*objectname)",
            "refs/tags/",
        )
        tags: dict[str, str] = {}
        for line in local.stdout.splitlines():
            name, target, peeled = line.split(sep=" ", maxsplit=2)
            tags[name] = peeled or target
        tags.update(self._read_remote_tags(remote=remote))
        return tags

    def _read_remote_tags(
        self, *, remote: str, refs: tuple[str, ...] = ()
    ) -> dict[str, str]:
        """Read remote tag targets, resolving annotated tags to their peeled objects.

        :param remote: the configured authority.
        :param refs: the tag refs to select, empty for all remote tags.

        :return: tag names mapped to their resolved target object IDs.
        """
        targets: dict[str, str] = {}
        for line in self._command_runner.run_command(
            "ls-remote", "--tags", "--", remote, *refs
        ).stdout.splitlines():
            target, ref = line.split(sep="\t", maxsplit=1)
            targets[ref] = target
        return {
            ref.removeprefix("refs/tags/"): targets.get(f"{ref}^{{}}", target)
            for ref, target in targets.items()
            if not ref.endswith("^{}")
        }

    def validate_tag(self, *, name: str) -> None:
        """Validate a literal release tag name before publication.

        :param name: the complete tag name including its configured prefix.
        """
        self._command_runner.run_command("check-ref-format", f"refs/tags/{name}")

    def publish_tag(self, *, name: str, commit: str, remote: str) -> None:
        """Create or resume publishing one tag without replacing an existing target.

        :param name: the tag name and annotation message.
        :param commit: the verified integration commit ID.
        :param remote: the configured publication authority.

        :raises WorkflowError: if an existing local or remote tag has another target.
        """
        self.validate_tag(name=name)
        remote_tags = self._read_remote_tags(
            remote=remote, refs=(f"refs/tags/{name}", f"refs/tags/{name}^{{}}")
        )
        if name in remote_tags:
            if remote_tags[name] != commit:
                raise WorkflowError(f"Release tag points to another commit: {name}")
            return
        existing = self._command_runner.run_command(
            "rev-parse", "--verify", f"refs/tags/{name}^{{commit}}", allowed=(0, 128)
        )
        if existing.returncode == 0:
            if existing.stdout.strip() != commit:
                raise WorkflowError(f"Release tag points to another commit: {name}")
        else:
            self._command_runner.run_command(
                "tag",
                "--annotate",
                "--message",
                name,
                "--",
                name,
                commit,
                capture=False,
            )
        self._command_runner.run_command(
            "push", "--", remote, f"refs/tags/{name}:refs/tags/{name}", capture=False
        )
