"""Secret-scrubbing launcher for the Codex SDK app-server process."""

import os
import sys
from collections.abc import Mapping


ALLOWED_ENV = {
    "PATH",
    "Path",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "USERNAME",
    "CODEX_HOME",
    "LANG",
    "LC_ALL",
}


def sanitized_environment(
    source: Mapping[str, str],
    codex_home: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Copy only the non-secret variables needed by Codex and Git."""

    environment = {
        key: value for key, value in source.items() if key in ALLOWED_ENV
    }
    if codex_home is not None:
        environment["CODEX_HOME"] = os.fspath(codex_home)
    return environment


def prepare_process_group(platform: str = os.name) -> None:
    """Put the POSIX app-server in a killable, isolated process group."""

    if platform != "nt":
        os.setsid()


def main() -> int:
    executable = os.environ.get("PRDTOCASE_CODEX_BIN")
    if not executable:
        raise RuntimeError("PRDTOCASE_CODEX_BIN is required")
    child_env = sanitized_environment(os.environ)
    prepare_process_group()
    os.execve(executable, [executable, *sys.argv[1:]], child_env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
