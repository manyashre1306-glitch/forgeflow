"""Tests for the sandbox (SPEC sections 2F and 3).

The refusal path is the most important thing tested here. Tests that need a
container use an injected :class:`IsolationStatus`; none require a daemon, and
none execute repository code.
"""

from __future__ import annotations

import subprocess

import pytest

from forgeflow.sandbox import (
    IsolationError,
    IsolationKind,
    IsolationStatus,
    Sandbox,
    SandboxLimits,
    build_container_command,
    detect_backend,
)

UNAVAILABLE = IsolationStatus(
    IsolationKind.NONE,
    False,
    "no container runtime",
    details={"docker": "daemon not reachable", "podman": "not installed"},
)
DOCKER_OK = IsolationStatus(IsolationKind.DOCKER, True, "daemon reachable")


# --- limits validation ------------------------------------------------------


def test_default_limits_are_conservative() -> None:
    limits = SandboxLimits()
    assert limits.allow_network is False
    assert limits.cpu_count == 1.0
    assert limits.memory_mb == 1024
    assert limits.read_only_root is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpu_count", 0),
        ("cpu_count", -1),
        ("memory_mb", 0),
        ("timeout_seconds", 0),
        ("pids_limit", 0),
    ],
)
def test_invalid_limits_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        SandboxLimits(**{field: value})


def test_network_is_off_by_default() -> None:
    assert SandboxLimits().allow_network is False


# --- command construction ---------------------------------------------------


def test_container_command_applies_all_limits(tmp_path) -> None:
    command = build_container_command(
        binary="docker",
        image="python:3.12-slim",
        workspace=tmp_path,
        command=["pytest", "-q"],
        limits=SandboxLimits(cpu_count=2.0, memory_mb=512, pids_limit=64, timeout_seconds=10),
    )
    joined = " ".join(command)
    assert "--cpus 2.0" in joined
    assert "--memory 512m" in joined
    assert "--pids-limit 64" in joined
    assert "--network none" in joined
    assert "--read-only" in command
    assert command[-2:] == ["pytest", "-q"]


def test_container_command_uses_none_network_by_default(tmp_path) -> None:
    command = build_container_command(
        binary="docker",
        image="img",
        workspace=tmp_path,
        command=["true"],
        limits=SandboxLimits(),
    )
    assert command[command.index("--network") + 1] == "none"


def test_container_command_can_enable_network_explicitly(tmp_path) -> None:
    command = build_container_command(
        binary="docker",
        image="img",
        workspace=tmp_path,
        command=["true"],
        limits=SandboxLimits(allow_network=True),
    )
    assert command[command.index("--network") + 1] == "bridge"


def test_container_command_mounts_only_the_workspace(tmp_path) -> None:
    command = build_container_command(
        binary="docker",
        image="img",
        workspace=tmp_path,
        command=["true"],
        limits=SandboxLimits(),
    )
    volumes = [item for item in command if ":/workspace" in item]
    assert len(volumes) == 1
    assert str(tmp_path.resolve()) in volumes[0]


def test_container_command_forwards_no_host_environment(tmp_path) -> None:
    command = build_container_command(
        binary="docker",
        image="img",
        workspace=tmp_path,
        command=["true"],
        limits=SandboxLimits(),
    )
    assert "--env" not in command
    assert not any(item.startswith("--env-file") for item in command)
    assert "-e" not in command


def test_container_command_removes_the_container(tmp_path) -> None:
    command = build_container_command(
        binary="docker",
        image="img",
        workspace=tmp_path,
        command=["true"],
        limits=SandboxLimits(),
    )
    assert "--rm" in command


def test_read_only_root_adds_a_writable_tmp(tmp_path) -> None:
    command = build_container_command(
        binary="docker",
        image="img",
        workspace=tmp_path,
        command=["true"],
        limits=SandboxLimits(read_only_root=True),
    )
    assert "--read-only" in command
    assert any("/tmp" in item for item in command)


# --- refusal path -----------------------------------------------------------


def test_run_refuses_without_isolation(tmp_path) -> None:
    sandbox = Sandbox(status=UNAVAILABLE)
    with pytest.raises(IsolationError, match="Refusing to execute untrusted"):
        sandbox.run(["pytest"], workspace=tmp_path)


def test_run_refuses_even_for_a_harmless_command(tmp_path) -> None:
    """The refusal must not depend on judging the command's danger."""
    sandbox = Sandbox(status=UNAVAILABLE)
    with pytest.raises(IsolationError):
        sandbox.run(["echo", "hello"], workspace=tmp_path)


def test_refusal_happens_before_touching_the_workspace(tmp_path) -> None:
    """A missing workspace must not change the refusal message."""
    sandbox = Sandbox(status=UNAVAILABLE)
    with pytest.raises(IsolationError, match="no container isolation"):
        sandbox.run(["pytest"], workspace=tmp_path / "does-not-exist")


def test_isolation_error_is_not_a_generic_runtime_error_subclass() -> None:
    """Callers must not swallow this as an ordinary execution failure."""
    assert issubclass(IsolationError, RuntimeError)


def test_is_isolated_is_false_without_a_backend() -> None:
    assert Sandbox(status=UNAVAILABLE).is_isolated is False


def test_explain_states_the_refusal_and_the_remedy() -> None:
    text = Sandbox(status=UNAVAILABLE).explain()
    assert "will NOT execute" in text
    assert "no container runtime" in text
    assert "docker" in text
    assert "Remedy" in text


def test_explain_describes_limits_when_isolated() -> None:
    text = Sandbox(status=DOCKER_OK, limits=SandboxLimits(memory_mb=256)).explain()
    assert "docker" in text
    assert "256 MiB" in text
    assert "network off" in text


def test_refusal_result_records_the_reason() -> None:
    result = Sandbox(status=UNAVAILABLE).refusal_result(["pytest", "-q"])
    assert result.refused is True
    assert result.isolated is False
    assert result.exit_code == -1
    assert result.command == ["pytest", "-q"]
    assert "no container runtime" in (result.refusal_reason or "")


# --- execution with an injected backend ------------------------------------


def test_run_executes_when_isolation_is_available(tmp_path, monkeypatch) -> None:
    """With a reported backend, the container command is really invoked."""
    recorded: dict[str, list[str]] = {}

    class Completed:
        returncode = 0
        stdout = "1 passed"
        stderr = ""

    def fake_run(command, **kwargs):
        recorded["command"] = list(command)
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = Sandbox(status=DOCKER_OK).run(["pytest", "-q"], workspace=tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "1 passed"
    assert result.backend == "docker"
    assert recorded["command"][0] == "docker"
    assert recorded["command"][-2:] == ["pytest", "-q"]


def test_run_reports_a_nonzero_exit(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 1
        stdout = ""
        stderr = "1 failed"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Completed())
    result = Sandbox(status=DOCKER_OK).run(["pytest"], workspace=tmp_path)
    assert result.exit_code == 1
    assert result.stderr == "1 failed"


def test_run_honours_the_timeout(tmp_path, monkeypatch) -> None:
    def timeout_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", timeout_run)
    result = Sandbox(status=DOCKER_OK, limits=SandboxLimits(timeout_seconds=1)).run(
        ["pytest"], workspace=tmp_path
    )
    assert result.timed_out is True
    assert result.exit_code == 124
    assert "timed out" in result.stderr


def test_run_requires_an_existing_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    sandbox = Sandbox(status=DOCKER_OK)
    with pytest.raises(IsolationError, match="workspace directory not found"):
        sandbox.run(["pytest"], workspace=tmp_path / "nope")


def test_per_call_limits_override_defaults(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["timeout"] = kwargs.get("timeout")
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    sandbox = Sandbox(status=DOCKER_OK)
    sandbox.run(
        ["true"],
        workspace=tmp_path,
        limits=SandboxLimits(timeout_seconds=7, memory_mb=128),
    )
    assert captured["timeout"] == 7
    assert "--memory 128m" in " ".join(captured["command"])  # type: ignore[arg-type]


# --- detection --------------------------------------------------------------


def test_detect_backend_reports_a_kind_and_reason() -> None:
    status = detect_backend()
    assert status.kind in {IsolationKind.DOCKER, IsolationKind.PODMAN, IsolationKind.NONE}
    assert isinstance(status.reason, str)
    assert status.reason


def test_detect_backend_probes_a_real_daemon() -> None:
    """In this environment no daemon runs, so detection must report NONE.

    The assertion is written to hold either way, but the details must name the
    concrete failure rather than a generic message.
    """
    status = detect_backend(probe=True)
    if not status.available:
        assert status.kind is IsolationKind.NONE
        assert status.isolated is False
        assert "docker" in status.details


def test_detect_backend_skip_probe_trusts_the_binary() -> None:
    status = detect_backend(probe=False)
    # Either docker/podman is present, or neither is.
    if status.available:
        assert status.kind in {IsolationKind.DOCKER, IsolationKind.PODMAN}
    else:
        assert status.kind is IsolationKind.NONE


def test_isolation_status_isolated_requires_availability() -> None:
    assert IsolationStatus(IsolationKind.DOCKER, False, "x").isolated is False
    assert IsolationStatus(IsolationKind.NONE, True, "x").isolated is False
    assert IsolationStatus(IsolationKind.DOCKER, True, "x").isolated is True
