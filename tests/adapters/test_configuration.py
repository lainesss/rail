"""Exercise configuration installation, preservation, and input validation."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from rail.adapters.configuration import (
    add_configuration_ignore_rule,
    install_configuration,
    read_settings,
)
from rail.application.contracts import Check, GitSettings, WorkflowError
from tests.support.configuration import make_settings


@pytest.mark.parametrize(
    argnames="label", argvalues=["\U0001f9ea Check", "Control\x7f"]
)
def test_configuration_round_trips_unicode(*, tmp_path: Path, label: str) -> None:
    """Context: an installation tag prefix contains Unicode or a DEL character.

    Invariants:
        - Saved TOML preserves the selected repository and release choices.
        - Rendering does not emit invalid surrogate escapes or raw DEL characters.
    """
    settings = make_settings(prefix=label)
    install_configuration(root=tmp_path, settings=settings)
    saved = read_settings(root=tmp_path)
    assert saved.git == settings.git and saved.release == settings.release
    document = (tmp_path / "rail.toml").read_text(encoding="utf-8")
    assert "\\ud83e" not in document and "\x7f" not in document


def test_install_and_preserve(*, tmp_path: Path) -> None:
    """Context: setup runs first on a new project and then on customized config.

    Invariants:
        - Installed repository and release settings match the supplied choices.
        - Reading a manually customized file preserves its settings.
        - Explicit selections are saved and report only actual changed lines.
        - Installation creates root-level rail.toml without a settings directory.
        - Installation creates no command reference.
        - New configuration includes the Pre-commit check.
    """
    created, difference = install_configuration(root=tmp_path, settings=make_settings())
    assert created
    assert (
        '+   { name = "Pre-commit", command = "pre-commit run --all-files", '
        "retries = 1 },"
    ) in difference
    assert read_settings(root=tmp_path) == make_settings(
        checks=(
            Check(
                name="Pre-commit",
                args=("pre-commit", "run", "--all-files"),
                retries=1,
            ),
        )
    )
    config = tmp_path / "rail.toml"
    assert config.is_file()
    assert not (tmp_path / ".rail").exists()
    reference = tmp_path / ".rail/COMMANDS.md"
    assert not reference.exists()
    custom = (
        '[git]\nremote = "central"\nmain = "trunk"\n'
        "[checks]\ncommands = [\n"
        '{name="Script", command="python script.py \'\'", retries=0}]\n'
        '[release]\npolicy = "calver"\nprefix = ""\n'
    )
    config.write_text(data=custom, encoding="utf-8")
    assert config.read_text(encoding="utf-8") == custom
    assert not reference.exists()
    assert read_settings(root=tmp_path) == make_settings(
        remote="central",
        main="trunk",
        checks=(Check(name="Script", args=("python", "script.py", ""), retries=0),),
        policy="calver",
        prefix="",
    )
    selected = make_settings(
        remote="central", main="trunk", policy="calver", prefix="r"
    )
    created, difference = install_configuration(root=tmp_path, settings=selected)
    assert not created
    saved = read_settings(root=tmp_path)
    assert saved.git == selected.git and saved.release == selected.release
    assert saved.checks.commands[0].args == ("pre-commit", "run", "--all-files")
    assert '- prefix = ""\n' in difference
    assert '+ prefix = "r"\n' in difference
    assert all(line.startswith(("- ", "+ ")) for line in difference.splitlines())


def test_read_settings_missing_file(*, tmp_path: Path) -> None:
    """Context: configuration does not exist.

    Invariants:
        - The file error identifies the missing configuration path.
    """
    with pytest.raises(expected_exception=FileNotFoundError, match="rail.toml"):
        read_settings(root=tmp_path)


def test_configuration_uses_only_root_file(*, tmp_path: Path) -> None:
    """Context: the project still has configuration at the previous location.

    Invariants:
        - Reading requires root-level rail.toml, even when the old file exists.
        - Installation writes and reads the selected settings in the root file.
        - Installation preserves the previous configuration verbatim.
    """
    previous = tmp_path / ".rail" / "config.toml"
    previous.parent.mkdir()
    previous.write_text(data="previous configuration\n", encoding="utf-8")
    with pytest.raises(expected_exception=FileNotFoundError, match="rail.toml"):
        read_settings(root=tmp_path)

    selected = make_settings(remote="upstream", main="trunk", policy="calver")
    created, _ = install_configuration(root=tmp_path, settings=selected)
    assert created
    saved = read_settings(root=tmp_path)
    assert saved.git == selected.git and saved.release == selected.release
    assert (tmp_path / "rail.toml").is_file()
    assert previous.read_text(encoding="utf-8") == "previous configuration\n"


def test_ignore_configuration_rejects_symlink(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: the filesystem reports the ignore file as a symbolic link.

    Invariants:
        - The existing file is preserved and ignore changes are rejected.
    """
    path = tmp_path / ".gitignore"
    path.write_text(data="keep", encoding="utf-8")
    monkeypatch.setattr(target=Path, name="is_symlink", value=lambda self: self == path)
    with pytest.raises(expected_exception=WorkflowError, match="symbolic"):
        add_configuration_ignore_rule(root=tmp_path)
    assert path.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    argnames="existing", argvalues=[None, "", "*.log", "*.log\n", "*.log\n\n"]
)
def test_configuration_ignore_block(*, tmp_path: Path, existing: str | None) -> None:
    """Context: setup is told to keep its configuration out of Git.

    Invariants:
        - Existing entries and newline separation precede the configuration-only rule.
        - Repeating the choice leaves the file unchanged.
    """
    path = tmp_path / ".gitignore"
    if existing is not None:
        path.write_text(data=existing, encoding="utf-8")
    add_configuration_ignore_rule(root=tmp_path)
    saved = path.read_text(encoding="utf-8")
    prefix = (existing.rstrip("\n") + "\n\n") if existing else ""
    assert saved == prefix + "# Workflow configuration\n/rail.toml\n"
    add_configuration_ignore_rule(root=tmp_path)
    assert path.read_text(encoding="utf-8") == saved


def test_configuration_ignore_rule_can_precede_other_entries(*, tmp_path: Path) -> None:
    """Context: the configuration ignore rule already occurs in the middle of the file.

    Invariants:
        - Installation preserves the file verbatim without duplicating the rule.
    """
    path = tmp_path / ".gitignore"
    document = "*.log\n/rail.toml\n# later rules\nbuild/\n"
    path.write_text(data=document, encoding="utf-8")
    add_configuration_ignore_rule(root=tmp_path)
    assert path.read_text(encoding="utf-8") == document


def test_install_writes_missing_release_section(*, tmp_path: Path) -> None:
    """Context: an incomplete file is replaced by explicitly supplied complete settings.

    Invariants:
        - Loading rejects the missing release section without filling it.
        - Saving complete settings writes the missing release section.
        - The reported additions describe the release settings actually saved.
    """
    install_configuration(root=tmp_path, settings=make_settings())
    path = tmp_path / "rail.toml"
    partial = path.read_text(encoding="utf-8").split("[release]", maxsplit=1)[0]
    path.write_text(data=partial, encoding="utf-8")
    with pytest.raises(expected_exception=ValidationError, match="release"):
        read_settings(root=tmp_path)
    selected = make_settings()
    created, difference = install_configuration(root=tmp_path, settings=selected)
    assert not created
    assert '[release]\npolicy = "semver"\nprefix = "v"' in path.read_text(
        encoding="utf-8"
    )
    assert "+ [release]\n" in difference
    assert '+ policy = "semver"\n' in difference


@pytest.mark.parametrize(argnames="retries", argvalues=[0, 1, 2])
def test_named_command_quoting(*, tmp_path: Path, retries: int) -> None:
    """Context: a named command uses quoted paths, empty arguments, and literal symbols.

    Invariants:
        - Names and per-command retry counts survive configuration loading.
        - Quoted spaces and backslashes are preserved as literal arguments.
        - Shell-like text is not expanded or treated as comments.
    """
    install_configuration(root=tmp_path, settings=make_settings())
    command = (
        "'C:\\Program Files\\Python\\python.exe' \"script with spaces.py\" "
        '"" "#literal" "$VALUE;touch"'
    )
    document = (
        '[checks]\ncommands = [{name="Named script", command="""'
        + command.replace("\\", "\\\\").replace('"', '\\"')
        + f'""", retries={retries}}}]\n'
    )
    config = tmp_path / "rail.toml"
    complete = config.read_text(encoding="utf-8")
    document = (
        complete.split("[checks]", maxsplit=1)[0]
        + document
        + "[release]"
        + complete.split("[release]", maxsplit=1)[1]
    )
    config.write_text(data=document, encoding="utf-8")
    assert read_settings(root=tmp_path).checks.commands == (
        Check(
            name="Named script",
            args=(
                "C:\\Program Files\\Python\\python.exe",
                "script with spaces.py",
                "",
                "#literal",
                "$VALUE;touch",
            ),
            retries=retries,
        ),
    )


def test_read_settings_rejects_malformed_toml(*, tmp_path: Path) -> None:
    """Context: the saved configuration cannot be decoded as TOML.

    Invariants:
        - Reading raises a TOML decoding error and preserves the source file.
    """
    path = tmp_path / "rail.toml"
    path.write_text(data="[", encoding="utf-8")
    with pytest.raises(expected_exception=tomllib.TOMLDecodeError):
        read_settings(root=tmp_path)
    assert path.read_text(encoding="utf-8") == "["


@pytest.mark.parametrize(
    argnames="section,contents,location",
    argvalues=[
        ("", "unknown=1", ("unknown",)),
        ("git", "git=1", ("git",)),
        ("git", '[git]\nremote="-bad"\nmain="main"', ("git", "remote")),
        ("git", '[git]\nremote="origin"\nmain="-bad"', ("git", "main")),
        ("git", '[git]\nmain="main"', ("git", "remote")),
        ("git", '[git]\nremote="origin"', ("git", "main")),
        ("checks", "[checks]", ("checks", "commands")),
        ("checks", "[checks]\ncommands=[]\nretry=1", ("checks", "retry")),
        ("checks", "[checks]\ncommands=1", ("checks", "commands")),
        ("checks", '[checks]\ncommands=["script"]', ("checks", "commands", 0)),
        ("checks", "[checks]\ncommands=[[]]", ("checks", "commands", 0)),
        ("release", '[release]\npolicy="other"\nprefix="v"', ("release", "policy")),
        ("release", '[release]\npolicy="semver"\nprefix=3', ("release", "prefix")),
        ("release", '[release]\nprefix="v"', ("release", "policy")),
        ("release", '[release]\npolicy="semver"', ("release", "prefix")),
    ],
)
def test_read_settings_rejects_invalid_fields(
    *,
    tmp_path: Path,
    section: str,
    contents: str,
    location: tuple[str | int, ...],
) -> None:
    """Context: one setting is invalid while all other required settings are valid.

    Invariants:
        - Validation identifies exactly the invalid field, without unrelated errors.
        - Reading preserves the invalid source file.
    """
    sections = {
        "git": '[git]\nremote="origin"\nmain="main"',
        "checks": "[checks]\ncommands=[]",
        "release": '[release]\npolicy="semver"\nprefix="v"',
    }
    if section:
        sections[section] = contents
        document = "\n".join(sections.values())
    else:
        document = contents + "\n" + "\n".join(sections.values())
    path = tmp_path / "rail.toml"
    path.write_text(data=document, encoding="utf-8")
    with pytest.raises(expected_exception=ValidationError) as failure:
        read_settings(root=tmp_path)
    assert [error["loc"] for error in failure.value.errors()] == [location]
    assert path.read_text(encoding="utf-8") == document


@pytest.mark.parametrize(
    argnames="command,field",
    argvalues=[
        ('name="Script", command="python", retries=3', "retries"),
        ('name="Script", command="python", retries=-1', "retries"),
        ('name="Script", command="python", retries=true', "retries"),
        ('name="Script", command="python", retries=1.0', "retries"),
        ('name="Script", command="python", retries="1"', "retries"),
        ('name="Script", command="python"', "retries"),
        ('command="python", retries=0', "name"),
        ('name="Script", retries=0', "args"),
        ('name=" ", command="python", retries=0', "name"),
        ('name="Script", command=" ", retries=0', "command"),
        ('name="Script", command="\'\'", retries=0', "command"),
        ('name="Script", command="\'unclosed", retries=0', "command"),
        ('name="Script", command="python", retries=0, extra=1', "extra"),
    ],
)
def test_read_settings_rejects_invalid_check(
    *, tmp_path: Path, command: str, field: str
) -> None:
    """Context: a complete configuration contains one invalid named check.

    Invariants:
        - Validation identifies only the intended field in the first check.
        - Reading preserves the invalid source file.
    """
    install_configuration(root=tmp_path, settings=make_settings())
    path = tmp_path / "rail.toml"
    current = path.read_text(encoding="utf-8")
    document = (
        current.split("[checks]", maxsplit=1)[0]
        + "[checks]\ncommands=[{"
        + command
        + "}]\n[release]"
        + current.split("[release]", maxsplit=1)[1]
    )
    path.write_text(data=document, encoding="utf-8")
    with pytest.raises(expected_exception=ValidationError) as failure:
        read_settings(root=tmp_path)
    assert [error["loc"] for error in failure.value.errors()] == [
        ("checks", "commands", 0, field)
    ]
    assert path.read_text(encoding="utf-8") == document


@pytest.mark.parametrize(argnames="section", argvalues=["git", "checks", "release"])
def test_read_settings_requires_every_section(*, tmp_path: Path, section: str) -> None:
    """Context: an otherwise valid configuration omits one required section.

    Invariants:
        - Reading reports the missing section instead of supplying defaults.
    """
    sections = {
        "git": '[git]\nremote="origin"\nmain="main"',
        "checks": "[checks]\ncommands=[]",
        "release": '[release]\npolicy="semver"\nprefix="v"',
    }
    del sections[section]
    path = tmp_path / "rail.toml"
    path.write_text(data="\n".join(sections.values()), encoding="utf-8")
    with pytest.raises(expected_exception=ValidationError) as failure:
        read_settings(root=tmp_path)
    assert [error["loc"] for error in failure.value.errors()] == [(section,)]


@pytest.mark.parametrize(argnames="value", argvalues=[1, "", " ", "bad\x00text"])
def test_invalid_text(*, value: object) -> None:
    """Context: an external setting is not an admissible nonblank string.

    Invariants:
        - Model validation reports the remote field.
    """
    with pytest.raises(expected_exception=ValidationError, match="remote"):
        GitSettings.model_validate(obj={"remote": value, "main": "main"})


def test_install_rejects_links(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: a configuration destination is reported as a symbolic link.

    Invariants:
        - Setup rejects the destination before writing configuration.
    """
    monkeypatch.setattr(
        target=Path, name="is_symlink", value=lambda self: self.name == "rail.toml"
    )
    with pytest.raises(expected_exception=WorkflowError, match="symbolic"):
        install_configuration(root=tmp_path, settings=make_settings())
    assert not (tmp_path / "rail.toml").exists()
