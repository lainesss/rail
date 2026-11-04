"""Install the built wheel and check its CLI and API outside the checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory


def check_installed_cli() -> None:
    """Install one wheel and exercise its CLI in a temporary environment.

    :raises RuntimeError: when prerequisites or observed CLI results are invalid.
    """
    project = Path(__file__).resolve().parents[1]
    wheels = list((project / "dist").glob(pattern="*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected one wheel in dist/. Clean dist/ and run uv build.")
    uv = shutil.which(cmd="uv")
    if uv is None:
        raise RuntimeError("uv is required to check the installed command")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = f"rail {metadata['project']['version']}\n"
    print(f"Candidate wheel: {wheels[0]}", flush=True)
    with TemporaryDirectory(prefix="cli-") as directory:
        consumer = Path(directory)
        if consumer.resolve().is_relative_to(project):
            raise RuntimeError("Set TEMP/TMP to a location outside the checkout.")
        virtualenv = consumer / "environment"
        environment["VIRTUAL_ENV"] = str(virtualenv)
        scripts = virtualenv / ("Scripts" if os.name == "nt" else "bin")
        for arguments in (
            [uv, "venv", "--python", sys.executable, str(virtualenv)],
            [uv, "pip", "install", "--python", str(virtualenv), str(wheels[0])],
        ):
            subprocess.run(
                args=arguments, cwd=consumer, env=environment, check=True, timeout=120
            )
        origin_check = (
            "import sys\n"
            "from pathlib import Path\n"
            "import rail\n"
            "from rail import api\n"
            "origin = Path(rail.__file__).resolve()\n"
            "if not origin.is_relative_to(Path(sys.prefix).resolve()):\n"
            "    raise SystemExit(f'Package was imported outside the venv: {origin}')\n"
            "print(f'Python: {sys.version}')\n"
            "print(f'Installed package: {origin}')\n"
            "expected = {\n"
            "    'install', 'inspect', 'fetch', 'create_branch',\n"
            "    'create_worktree', 'check', 'finish', 'release',\n"
            "}\n"
            "assert set(api.__all__) == expected\n"
            "assert all(callable(getattr(api, name)) for name in expected)\n"
            "assert api.inspect() == {'workspace': None}\n"
        )
        subprocess.run(
            args=[
                str(scripts / ("python.exe" if os.name == "nt" else "python")),
                "-I",
                "-c",
                origin_check,
            ],
            cwd=consumer,
            env=environment,
            check=True,
            timeout=30,
        )
        executable = "rail.exe" if os.name == "nt" else "rail"
        command = [str(scripts / executable)]
        for arguments, exit_code, expected_stdout in (
            (["--help"], 0, None),
            (["--version"], 0, expected_version),
            (["--unknown"], 2, ""),
        ):
            result = subprocess.run(
                args=[*command, *arguments],
                cwd=consumer,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if result.returncode != exit_code:
                raise RuntimeError(
                    f"{command} {arguments}: exit {result.returncode}\n"
                    f"{result.stdout}{result.stderr}"
                )
            if expected_stdout is not None and result.stdout != expected_stdout:
                raise RuntimeError(
                    f"{command} {arguments}: unexpected stdout\n"
                    f"{result.stdout!r}\n"
                    f"stderr: {result.stderr}"
                )
            if (exit_code == 0 and result.stderr) or (
                exit_code == 2 and "[RAIL] ERROR:" not in result.stderr
            ):
                raise RuntimeError(
                    f"{command} {arguments}: unexpected stderr {result.stderr!r}"
                )
        subprocess.run(
            args=["git", "init", "--initial-branch=main"],
            cwd=consumer,
            env=environment,
            check=True,
            timeout=30,
        )
        for answers in ("origin\nmain\nsemver\nv\n", "n\n"):
            subprocess.run(
                args=[*command, "install"],
                cwd=consumer,
                env=environment,
                input=answers,
                text=True,
                check=True,
                timeout=30,
            )
        configuration = consumer / "rail.toml"
        if not configuration.is_file():
            raise RuntimeError(
                "Installed package configuration was not generated correctly."
            )
        if (consumer / ".rail").exists():
            raise RuntimeError("Installation created the previous settings directory.")
        expected_configuration = {
            "git": {"remote": "origin", "main": "main"},
            "checks": {
                "commands": [
                    {
                        "name": "Pre-commit",
                        "command": "pre-commit run --all-files",
                        "retries": 1,
                    }
                ]
            },
            "release": {"policy": "semver", "prefix": "v"},
        }
        if (
            tomllib.loads(configuration.read_text(encoding="utf-8"))
            != expected_configuration
        ):
            raise RuntimeError(
                "Installed configuration does not match the setup choices."
            )
        # Consumer projects choose their checks; this fixture needs no project tools.
        configuration.write_text(
            data=(
                '[git]\nremote = "origin"\nmain = "main"\n'
                "[checks]\ncommands = []\n"
                '[release]\npolicy = "semver"\nprefix = "v"\n'
            ),
            encoding="utf-8",
        )
        subprocess.run(
            args=[*command, "check"],
            cwd=consumer,
            env=environment,
            check=True,
            timeout=30,
        )
    print("Installed CLI checks passed.")


if __name__ == "__main__":
    check_installed_cli()
