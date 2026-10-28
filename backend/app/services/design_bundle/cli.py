"""Running ``prism-kicad`` (cli-spec.md §3, §4, §7.3, §7.4, §9.1).

The CLI decides by exit code and stdout, never by stderr: stdout holds one
document, chosen by the exit code, which K validates against the committed
schema before using it. Every stderr line is kept for the job log; a line that
is not JSON is kept verbatim. Each run is bounded by the CLI's own
``timeout-ms`` plus 30 s, polls the job's cancel check every second, and
records its wall time and peak resident memory (``wait4``'s ``ru_maxrss``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.core.config import settings
from app.services import kicad_tools
from app.services.design_bundle import schemas
from app.services.design_bundle.generator import Generator
from app.services.process_tree import KILLED_EXIT_CODE, group_popen_kwargs, kill_tree

CLI_NAME = "prism-kicad"
OUTER_TIMEOUT_MARGIN_SECONDS = 30
_POLL_SECONDS = 0.2
_CANCEL_SECONDS = 1.0

#: §4: the stdout document of each exit code.
_DOCUMENTS = {
    0: None,  # the command's own result
    9: "prism.cli.build_result.a0",
    8: "prism.cli.verify_report.a0",
    10: "prism.cli.project_absent.a0",
}
_PERMANENT_EXITS = frozenset({2, 3, 4, 7})


class CliUnavailable(RuntimeError):
    """``prism-kicad`` is not installed where K looks."""


class CliFailure(RuntimeError):
    """An exit of 1-7, a kill, or a document that breaks the contract.

    ``retryable`` follows cli-spec.md §4: internal, timeout, output I/O and a
    killed process are retried; usage, input, resource-limit and precondition
    failures are K defects or need a changed limit, and are not.
    """

    def __init__(self, message: str, *, exit_code: int, retryable: bool, error: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.retryable = retryable
        self.error = dict(error or {})

    @property
    def code(self) -> str:
        return str(self.error.get("code") or f"exit-{self.exit_code}")


@dataclass(frozen=True)
class CliRun:
    argv: tuple[str, ...]
    exit_code: int
    document: dict[str, Any] | None
    stderr_lines: tuple[Any, ...] = ()
    elapsed_ms: int = 0
    peak_rss_bytes: int = 0
    extras: Mapping[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """What the job records per invocation (cli-spec.md §7.4): never inside a bundle."""

        return {
            "command": " ".join(self.argv[1:3]),
            "exit": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
            "peak_rss_bytes": self.peak_rss_bytes,
        }


def locate() -> str | None:
    configured = settings.PRISM_DESIGN_BUNDLE_CLI_PATH.strip()
    if configured:
        return configured
    return shutil.which(CLI_NAME)


def executable() -> str:
    path = locate()
    if not path:
        raise CliUnavailable(f"{CLI_NAME} is not installed (set PRISM_DESIGN_BUNDLE_CLI_PATH)")
    return path


def _parse_stderr(text: str) -> tuple[Any, ...]:
    lines: list[Any] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            lines.append(json.loads(line))
        except ValueError:
            lines.append(line)  # a runtime message before the panic hook; kept verbatim
    return tuple(lines)


def run(
    args: Sequence[str],
    *,
    timeout_seconds: float,
    expect: str | None,
    cancel: Callable[[], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> CliRun:
    """Run one invocation; ``expect`` is the schema of the exit-0 document.

    Returns for exits 0, 8, 9 and 10; raises :class:`CliFailure` otherwise.
    """

    result, stdout = _execute(args, timeout_seconds=timeout_seconds, cancel=cancel, env=env)
    return _interpret(result, stdout, expect)


def run_plain(
    args: Sequence[str],
    *,
    timeout_seconds: float,
    cancel: Callable[[], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> CliRun:
    """Run one invocation whose exit-0 stdout is plain JSON, not a contract document.

    ``prism-kicad system step`` (CONTRACTS.md §18) prints ``{products, missing,
    bytes}``; that result has no committed schema. Every non-zero exit is still
    interpreted exactly as :func:`run` does.
    """

    result, stdout = _execute(args, timeout_seconds=timeout_seconds, cancel=cancel, env=env)
    if result.exit_code == 0:
        document = _document(stdout)
        if document is None:
            raise CliFailure(
                f"{CLI_NAME} exited 0 without its JSON document", exit_code=0, retryable=True
            )
        return CliRun(**{**result.__dict__, "document": document})
    return _interpret(result, stdout, None)


def _execute(
    args: Sequence[str],
    *,
    timeout_seconds: float,
    cancel: Callable[[], None] | None,
    env: Mapping[str, str] | None,
) -> tuple[CliRun, bytes]:
    """Run one process; return its run record and raw stdout."""

    argv = (executable(), *(str(part) for part in args))
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
            **group_popen_kwargs(),  # its own process group: a kill reaches git and children too
            close_fds=True,
        )
    except OSError as error:
        raise CliUnavailable(f"{CLI_NAME} could not be executed: {error}") from error
    chunks: dict[str, list[bytes]] = {"out": [], "err": []}
    readers = [
        threading.Thread(target=lambda: chunks["out"].append(process.stdout.read()), daemon=True),
        threading.Thread(target=lambda: chunks["err"].append(process.stderr.read()), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = started + timeout_seconds
    next_cancel = started + _CANCEL_SECONDS
    finished = None
    try:
        while finished is None:
            finished = kicad_tools.wait_process(process, _POLL_SECONDS)
            if finished is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                raise CliFailure(f"{CLI_NAME} {args[0]} ran past {timeout_seconds:g}s", exit_code=KILLED_EXIT_CODE, retryable=True)
            if cancel is not None and now >= next_cancel:
                next_cancel = now + _CANCEL_SECONDS
                cancel()
    except BaseException:
        kill_tree(process)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=10)
    exit_code, peak_rss_bytes = finished
    stdout = b"".join(chunks["out"])
    stderr = b"".join(chunks["err"]).decode("utf-8", "replace")
    result = CliRun(
        argv=argv,
        exit_code=exit_code,
        document=None,
        stderr_lines=_parse_stderr(stderr),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        peak_rss_bytes=peak_rss_bytes,
    )
    return result, stdout


def _document(stdout: bytes) -> dict[str, Any] | None:
    if not stdout.strip():
        return None
    try:
        document = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _interpret(result: CliRun, stdout: bytes, expect: str | None) -> CliRun:
    code = result.exit_code
    document = _document(stdout)
    if code in (0, 8, 9, 10):
        wanted = expect if code == 0 else _DOCUMENTS[code]
        if document is None or wanted is None:
            raise CliFailure(f"{CLI_NAME} exited {code} without its document", exit_code=code, retryable=True)
        try:
            schemas.validate(document, wanted)
        except schemas.SchemaInvalid as error:
            raise CliFailure(f"{CLI_NAME} printed an invalid {wanted}: {error}", exit_code=code, retryable=True) from None
        return CliRun(**{**result.__dict__, "document": document})
    error_document: dict[str, Any] = {}
    if document is not None and document.get("schema") == "prism.cli.error.a0":
        try:
            schemas.validate(document, "prism.cli.error.a0")
            error_document = document
        except schemas.SchemaInvalid:
            error_document = {}
    message = str(error_document.get("message") or f"{CLI_NAME} exited {code}")
    if code in _PERMANENT_EXITS:
        raise CliFailure(message, exit_code=code, retryable=False, error=error_document)
    # 1, 5, 6, a signal (negative), or a code outside §4's table: internal, retried.
    raise CliFailure(message, exit_code=code, retryable=True, error=error_document)


# ------------------------------------------------------------------ version

_generators: dict[tuple[str, float], Generator] = {}
_generator_lock = threading.Lock()


def generator() -> Generator:
    """``prism-kicad version``, validated; cached per binary and its mtime."""

    path = executable()
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        stamp = 0.0
    with _generator_lock:
        cached = _generators.get((path, stamp))
    if cached is not None:
        return cached
    done = run(["version"], timeout_seconds=60, expect="prism.cli.version.a0")
    found = Generator(done.document or {})
    with _generator_lock:
        _generators[(path, stamp)] = found
    return found


_digests: dict[tuple[str, float, int], str] = {}


def binary_digest() -> str:
    """SHA-256 of the ``prism-kicad`` executable; cached per binary, its mtime and size.

    The generator id names what the bundles hold, so a fix to ``compare`` that
    leaves every member alone keeps it. A cached comparison is keyed by this
    digest too, so any new binary computes its comparisons afresh.
    """

    path = executable()
    try:
        status = os.stat(path)
        stamp = (path, status.st_mtime, status.st_size)
    except OSError:
        stamp = (path, 0.0, 0)
    with _generator_lock:
        cached = _digests.get(stamp)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(path, "rb") as binary:
        for chunk in iter(lambda: binary.read(1 << 20), b""):
            digest.update(chunk)
    found = digest.hexdigest()
    with _generator_lock:
        _digests[stamp] = found
    return found


def upgraded_args(upgraded: Sequence[Mapping[str, Any]]) -> list[str]:
    """One ``--upgraded <path>=<version>`` per source K upgraded (cli-spec.md §5.2.2, §9.4)."""

    args: list[str] = []
    for item in upgraded:
        args += ["--upgraded", f"{item['path']}={int(item['upgraded_from_version'])}"]
    return args


def limits(stage: str) -> list[str]:
    """``--limit`` arguments for one stage (cli-spec.md §7.2, K's settings)."""

    memory = {
        "parse": settings.PRISM_DESIGN_BUNDLE_PARSE_MEMORY_BYTES,
        "board": settings.PRISM_DESIGN_BUNDLE_BOARD_MEMORY_BYTES,
        "models": settings.PRISM_DESIGN_BUNDLE_MODELS_MEMORY_BYTES,
    }[stage]
    return ["--limit", f"max-memory-bytes={memory}", "--limit", f"timeout-ms={timeout_ms(stage)}"]


def timeout_ms(stage: str) -> int:
    return {
        "parse": settings.PRISM_DESIGN_BUNDLE_PARSE_TIMEOUT_MS,
        "board": settings.PRISM_DESIGN_BUNDLE_BOARD_TIMEOUT_MS,
        "models": settings.PRISM_DESIGN_BUNDLE_MODELS_TIMEOUT_MS,
    }[stage]


def outer_timeout(stage: str) -> float:
    return timeout_ms(stage) / 1000 + OUTER_TIMEOUT_MARGIN_SECONDS


def verify_outer_timeout() -> float:
    """The outer bound for ``bundle verify``, which has its own setting (C2r §2 item 9)."""

    return settings.PRISM_DESIGN_BUNDLE_VERIFY_TIMEOUT_MS / 1000 + OUTER_TIMEOUT_MARGIN_SECONDS
