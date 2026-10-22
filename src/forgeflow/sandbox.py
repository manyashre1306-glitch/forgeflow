"""Isolated execution for ForgeFlow Phase 2 (SPEC sections 3 and 2F).

The rule this module enforces: **untrusted repository code runs only inside a
real container, and if no container is available, nothing runs at all.**

Why that is not negotiable. Implementing an issue means executing code from a
repository ForgeFlow does not control: its test suite, its build scripts, its
conftest imports. Any of those can be hostile. Running them directly on the host
would give that code the operator's files, their SSH keys, their environment,
and their network. There is no partial-credit version of this, so the fallback
is refusal rather than a best-effort attempt.

Measured in this environment: the Docker *client* is installed but the daemon is
not reachable (``/var/run/docker.sock`` does not exist), and ``podman`` is
absent. :func:`detect_backend` therefore returns
:class:`IsolationUnavailable`, and :meth:`Sandbox.run` raises
:class:`IsolationError` instead of executing anything.

Resource limits are applied when a container *is* available. Network is disabled
by default. No secret is ever passed in: :meth:`Sandbox.run` accepts only a
command, a working directory, and limits, and the container is given no
credential-bearing environment.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

DEFAULT_IMAGE = "python:3.12-slim"


class IsolationKind(StrEnum):
    """How a command would be isolated."""

    DOCKER = "docker"
    PODMAN = "podman"
    NONE = "none"


class IsolationError(RuntimeError):
    """Raised when isolation is required but unavailable.

    Deliberately not a subclass of a generic "run failed" error: a caller must
    not be able to catch this as a routine execution failure and continue as
    though the command merely failed.
    """


@dataclass(frozen=True)
class SandboxLimits:
    """Resource limits for one isolated run."""

    cpu_count: float = 1.0
    memory_mb: int = 1024
    timeout_seconds: float = 300.0
    pids_limit: int = 256
    allow_network: bool = False
    read_only_root: bool = True

    def __post_init__(self) -> None:
        if self.cpu_count <= 0:
            raise ValueError("cpu_count must be positive")
        if self.memory_mb <= 0:
            raise ValueError("memory_mb must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.pids_limit <= 0:
            raise ValueError("pids_limit must be positive")


@dataclass(frozen=True)
class SandboxResult:
    """The outcome of one isolated run."""

    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    backend: str = "none"
    isolated: bool = True
    refused: bool = False
    refusal_reason: str | None = None


@dataclass(frozen=True)
class IsolationStatus:
    """Whether real isolation is available, and why not when it is not."""

    kind: IsolationKind
    available: bool
    reason: str
    details: dict[str, str] = field(default_factory=dict)

    @property
    def isolated(self) -> bool:
        return self.available and self.kind is not IsolationKind.NONE


def _binary_available(name: str) -> bool:
    return shutil.which(name) is not None


def _daemon_reachable(binary: str, timeout: float = 20.0) -> tuple[bool, str]:
    """Return (reachable, detail) for a container daemon.

    Runs ``<binary> info`` and reports the real error text when it fails, so the
    operator sees the actual cause instead of a generic message.
    """
    try:
        completed = subprocess.run(
            [binary, "info"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, f"{binary} is not installed"
    except subprocess.TimeoutExpired:
        return False, f"{binary} info timed out after {timeout}s"
    if completed.returncode == 0:
        return True, f"{binary} daemon reachable"
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    first = detail[-1] if detail else f"exit {completed.returncode}"
    return False, first


def detect_backend(*, probe: bool = True) -> IsolationStatus:
    """Detect the best available isolation.

    Checks Docker first, then Podman. When ``probe`` is true the daemon is
    actually contacted, because the CLI being on PATH does not mean a daemon is
    running; that distinction is exactly what makes this environment unsafe.
    """
    if _binary_available("docker"):
        if not probe:
            return IsolationStatus(IsolationKind.DOCKER, True, "docker CLI present (not probed)")
        reachable, detail = _daemon_reachable("docker")
        if reachable:
            return IsolationStatus(IsolationKind.DOCKER, True, detail)
        docker_detail = detail
    else:
        docker_detail = "docker is not installed"

    if _binary_available("podman"):
        if not probe:
            return IsolationStatus(IsolationKind.PODMAN, True, "podman CLI present (not probed)")
        reachable, detail = _daemon_reachable("podman")
        if reachable:
            return IsolationStatus(IsolationKind.PODMAN, True, detail)
        podman_detail = detail
    else:
        podman_detail = "podman is not installed"

    return IsolationStatus(
        IsolationKind.NONE,
        False,
        ("No usable container runtime: refusing to execute untrusted repository code on the host."),
        details={"docker": docker_detail, "podman": podman_detail},
    )


def build_container_command(
    *,
    binary: str,
    image: str,
    workspace: Path,
    command: list[str],
    limits: SandboxLimits,
) -> list[str]:
    """Build the container invocation for one run.

    Kept separate from execution so the limits can be asserted in tests without
    a daemon. The workspace is mounted read-write at ``/workspace``; nothing
    else from the host is mounted, and no host environment is forwarded.
    """
    args = [
        binary,
        "run",
        "--rm",
        "--workdir",
        "/workspace",
        "--volume",
        f"{workspace.resolve()}:/workspace",
        "--cpus",
        str(limits.cpu_count),
        "--memory",
        f"{limits.memory_mb}m",
        "--pids-limit",
        str(limits.pids_limit),
        "--network",
        "bridge" if limits.allow_network else "none",
    ]
    if limits.read_only_root:
        # The workspace stays writable; only the container's own root is read-only.
        args += ["--read-only", "--tmpfs", "/tmp:rw,size=64m"]
    args += [image, *command]
    return args


class Sandbox:
    """Runs commands in a container, or refuses when it cannot.

    The refusal is the important part of this class. It is not a soft failure
    and it is not configurable away.
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        limits: SandboxLimits | None = None,
        status: IsolationStatus | None = None,
    ) -> None:
        self._image = image
        self._limits = limits or SandboxLimits()
        self._status = status if status is not None else detect_backend()

    @property
    def limits(self) -> SandboxLimits:
        return self._limits

    @property
    def status(self) -> IsolationStatus:
        return self._status

    @property
    def is_isolated(self) -> bool:
        return self._status.isolated

    def explain(self) -> str:
        """A human-readable statement of what this sandbox will do."""
        if self._status.isolated:
            return (
                f"Container isolation available via {self._status.kind.value}. "
                f"Limits: {self._limits.cpu_count} CPU, "
                f"{self._limits.memory_mb} MiB, {self._limits.timeout_seconds}s, "
                f"network {'on' if self._limits.allow_network else 'off'}."
            )
        lines = [
            "No container isolation is available, so ForgeFlow will NOT execute repository code.",
            f"Reason: {self._status.reason}",
        ]
        for name, detail in self._status.details.items():
            lines.append(f"  {name}: {detail}")
        lines.append(
            "Remedy: start a Docker daemon (or install podman) and re-run. Until "
            "then, only MockAgent results are produced, and no repository code "
            "is run."
        )
        return "\n".join(lines)

    def run(
        self,
        command: list[str],
        *,
        workspace: Path | str,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        """Run ``command`` in the workspace inside a container.

        Raises :class:`IsolationError` when no isolation is available. That is
        the whole point: there is no code path here that falls back to running
        the command on the host.
        """
        effective = limits or self._limits

        if not self._status.isolated:
            raise IsolationError(
                "Refusing to execute untrusted repository code: no container "
                f"isolation is available ({self._status.reason})."
            )

        workspace_path = Path(workspace)
        if not workspace_path.is_dir():
            raise IsolationError(f"workspace directory not found: {workspace_path}")

        full_command = build_container_command(
            binary=self._status.kind.value,
            image=self._image,
            workspace=workspace_path,
            command=command,
            limits=effective,
        )

        started = time.monotonic()
        try:
            completed = subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                timeout=effective.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            return SandboxResult(
                command=command,
                exit_code=124,
                stdout=(exc.stdout or b"").decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or ""),
                stderr=f"timed out after {effective.timeout_seconds}s",
                duration_seconds=elapsed,
                timed_out=True,
                backend=self._status.kind.value,
            )

        return SandboxResult(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_seconds=time.monotonic() - started,
            backend=self._status.kind.value,
        )

    def refusal_result(self, command: list[str]) -> SandboxResult:
        """Return an explicit refusal record instead of raising.

        Used by the pipeline so a review report can state plainly that nothing
        was executed, rather than the run appearing to have simply produced no
        output.
        """
        return SandboxResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=self._status.reason,
            duration_seconds=0.0,
            backend=self._status.kind.value,
            isolated=False,
            refused=True,
            refusal_reason=self._status.reason,
        )


def build_sandbox(
    *,
    image: str = DEFAULT_IMAGE,
    limits: SandboxLimits | None = None,
) -> Sandbox:
    """Construct a sandbox with detected isolation."""
    return Sandbox(image=image, limits=limits, status=detect_backend())
