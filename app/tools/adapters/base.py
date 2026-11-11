"""Shared result and bounded subprocess execution for EDA adapters."""

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, JsonValue


EDAStatus = Literal["PASS", "FAIL", "ERROR", "TIMEOUT"]
MAX_LOG_BYTES = 8_000_000


class EDAToolResult(BaseModel):
    tool_name: str
    tool_version: str
    input_hash: str
    status: EDAStatus
    exit_code: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    artifacts: list[dict[str, JsonValue]] = Field(default_factory=list)
    issues: list[dict[str, JsonValue]] = Field(default_factory=list)
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    commands: list[list[str]] = Field(default_factory=list)
    duration_seconds: float = 0


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    resource_exceeded: bool = False


def run_process(command: list[str], cwd: Path, timeout: float) -> ProcessResult:
    """Use a fixed argv and cap execution time, log size and scratch output size."""
    from time import monotonic

    stdout_path, stderr_path = cwd / ".stdout", cwd / ".stderr"
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
        "VERILATOR_ROOT", "LANG", "LC_ALL",
    }}
    started = monotonic()
    timed_out = resource_exceeded = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            env=env, shell=False, start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            while process.poll() is None:
                timed_out = monotonic() - started >= timeout
                output_bytes = 0
                for item in cwd.rglob("*"):
                    try:
                        if item.is_file():
                            output_bytes += item.stat().st_size
                    except FileNotFoundError:
                        continue
                resource_exceeded = (
                    output_bytes > 128_000_000 or stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_LOG_BYTES
                )
                if timed_out or resource_exceeded:
                    break
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            [str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                             "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, timeout=5, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    except (KeyError, OSError, subprocess.TimeoutExpired):
                        pass
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait()
    with stdout_path.open("rb") as source:
        out = source.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
    with stderr_path.open("rb") as source:
        err = source.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
    if resource_exceeded:
        err += "\nEDA resource limit exceeded."
    if timed_out:
        err += "\nEDA execution timed out."
    return ProcessResult(124 if timed_out else 1 if resource_exceeded else process.returncode, out, err, timed_out, resource_exceeded)
