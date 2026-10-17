"""Which ephemeral smoke-test stacks belong to a run that is gone (shared by both suites).

Each run of a smoke suite starts its own Docker Compose project, named after the PID of the
pytest process that owns it (``<prefix><pid>-<random>``), so two runs (a VS Code run and a
terminal run, two worktrees) never share or remove each other's stack. A run killed before
its teardown leaves its containers behind; the next run removes the stacks whose owner
process no longer exists, and never the stack of a run still going.
"""

import os
import re
import secrets
import subprocess
import sys


def project_pattern(prefix: str) -> re.Pattern[str]:
    """``<prefix><pid>-<hex>``, with the owner PID in the ``pid`` group."""
    return re.compile(rf"{re.escape(prefix)}(?P<pid>\d+)-[0-9a-f]+")


def new_project(prefix: str) -> str:
    """A project name owned by this process."""
    return f"{prefix}{os.getpid()}-{secrets.token_hex(4)}"


if sys.platform == "win32":
    import ctypes

    def process_alive(pid: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, not ours
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

else:

    def process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)  # signal 0: only checks that the process exists
        except ProcessLookupError:
            return False
        except PermissionError:  # it exists, owned by another user
            return True
        return True


def stale_projects(pattern: re.Pattern[str]) -> list[str]:
    """The Compose projects matching ``pattern`` whose owner process is gone."""
    listed = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",  # noqa: S607
         "--format", '{{.Label "com.docker.compose.project"}}'],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
    ).stdout.split()  # fmt: skip
    return sorted(
        project
        for project in set(listed)
        if (match := pattern.fullmatch(project)) and not process_alive(int(match["pid"]))
    )
