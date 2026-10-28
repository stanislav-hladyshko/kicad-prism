"""Starting a child in its own process group and killing the whole tree, on POSIX and Windows."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

IS_WINDOWS = sys.platform == "win32"

#: Exit code reported for a child that was killed.
KILLED_EXIT_CODE = -signal.SIGTERM if IS_WINDOWS else -signal.SIGKILL


def group_popen_kwargs() -> dict[str, Any]:
    """``Popen`` arguments that give the child its own group, so a kill reaches its children too."""

    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_group(process: subprocess.Popen[Any]) -> None:
    """Kill ``process`` and everything it started, without reaping it. Never raises."""

    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except BaseException:  # noqa: BLE001 - fall back to the process alone
            pass
    try:
        process.kill()
    except BaseException:  # noqa: BLE001 - already gone
        pass


def kill_tree(process: subprocess.Popen[Any]) -> None:
    """Kill ``process`` and everything it started, then reap it."""

    kill_group(process)
    if IS_WINDOWS:
        process.wait()
        return
    try:
        os.waitpid(process.pid, 0)
    except ChildProcessError:
        pass
    process.returncode = -signal.SIGKILL
