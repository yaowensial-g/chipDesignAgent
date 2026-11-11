"""Compile, lint and run manifest-declared SystemVerilog testbenches."""

import os
import re
import shutil
from pathlib import Path
from time import monotonic

from app.tools.adapters.base import EDAToolResult, EDAStatus, ProcessResult, run_process
from app.tools.project_manifest import FrozenProject


SIMULATION_TOOLS = frozenset({"run_simulation", "run_testcase", "run_test_suite", "run_regression", "run_impacted_tests"})
VERILATOR_TOOLS = frozenset({"run_compile", "run_lint", *SIMULATION_TOOLS})
LINT_FLAGS = ("--lint-only", "-Wall")
SAFE_SYSTEM_CALLS = frozenset({
    "display", "write", "monitor", "strobe", "fatal", "error", "warning", "info", "finish",
    "stop", "dumpfile", "dumpvars", "time", "realtime", "timeformat", "clog2", "bits", "size",
    "signed", "unsigned", "isunknown", "random", "urandom", "urandom_range", "past", "rose",
    "fell", "stable", "onehot", "onehot0", "countones", "countbits", "sformatf", "value$plusargs",
    "test$plusargs", "asserton", "assertoff",
})


def simulation_environment(executable: str) -> dict:
    compiler = shutil.which("g++")
    configured = os.getenv("VERILATOR_ROOT")
    roots = (
        Path(configured),
    ) if configured else (Path(executable).parent.parent / "share" / "verilator", Path("/usr/share/verilator"))
    include = next((root / "include" for root in roots if (root / "include" / "verilated.cpp").is_file()), None)
    if compiler is None or include is None:
        raise ValueError("Simulation requires g++ and Verilator runtime headers (VERILATOR_ROOT)")
    compiler_path = Path(compiler).resolve()
    return {
        "compiler": str(compiler_path), "compiler_mtime_ns": compiler_path.stat().st_mtime_ns,
        "runtime_include": str(include.resolve()),
    }


def execute(
    tool: str, executable: str, version: str, project: FrozenProject, cases: tuple[str, ...], seed: int, root: Path,
    *, coverage: bool = False,
) -> tuple[EDAToolResult, str, str, dict[str, bytes]]:
    started = monotonic()
    commands: list[list[str]] = []
    outs: list[str] = []
    errs: list[str] = []
    outputs: dict[str, bytes] = {}
    case_results = []
    status: EDAStatus = "ERROR"
    code = None
    deadline = started + 300
    try:
        if tool not in VERILATOR_TOOLS:
            raise ValueError("Unsupported Verilator operation")
        if tool in SIMULATION_TOOLS:
            _check_test_sources(project)
            environment = simulation_environment(executable)
            for case in cases:
                name = project.manifest.testcases[case].top_module
                directory = root / case
                _materialize(project, directory)
                generate = _command(
                    project, executable, name, "--cc", "--main", "--timing", "--trace-vcd", "--assert",
                    "--prefix", "Vchipflow",
                    *(("--coverage",) if coverage else ()),
                )
                generate.extend(project.manifest.testcases[case].sources)
                compiled = _run(generate, directory, min(60, deadline - monotonic()), commands, outs, errs, f"{case}:verilate")
                case_status = _status(compiled, failed="ERROR")
                code = compiled.exit_code
                suffix = ".exe" if os.name == "nt" else ""
                binary = directory / "obj_dir" / f"chipflow_sim{suffix}"
                if case_status == "PASS":
                    build = _build_command(directory, environment, binary.name, coverage=coverage)
                    compiled = _run(build, directory, min(180, deadline - monotonic()), commands, outs, errs, f"{case}:build")
                    case_status = _status(compiled, failed="ERROR")
                    code = compiled.exit_code
                if case_status == "PASS":
                    if not binary.is_file():
                        raise ValueError("Verilator did not produce the expected simulation executable")
                    run_args = [str(binary), f"+verilator+seed+{seed}"]
                    if coverage:
                        run_args.append("+verilator+coverage+file+coverage.dat")
                    finished = _run(run_args, directory, min(30, deadline - monotonic()), commands, outs, errs, f"{case}:simulation")
                    code = finished.exit_code
                    combined = finished.stdout + "\n" + finished.stderr
                    case_status = _status(finished, failed="FAIL" if "%Error" in combined or "CHIPFLOW_TEST_FAIL" in combined else "ERROR")
                    if case_status == "PASS":
                        if "CHIPFLOW_TEST_FAIL" in combined:
                            case_status = "FAIL"
                        elif not re.search(r"(?m)^CHIPFLOW_TEST_PASS\s*$", finished.stdout):
                            case_status = "ERROR"
                            errs.append(f"{case}: no explicit CHIPFLOW_TEST_PASS marker")
                wave = directory / "wave.vcd"
                if wave.is_file() and not wave.is_symlink():
                    if wave.stat().st_size <= 8_000_000:
                        outputs[f"{case}.vcd"] = wave.read_bytes()
                    else:
                        errs.append(f"{case}: VCD exceeds the waveform size limit")
                        case_status = "ERROR"
                coverage_file = directory / "coverage.dat"
                if coverage and coverage_file.is_file() and not coverage_file.is_symlink() and coverage_file.stat().st_size <= 8_000_000:
                    outputs[f"{case}.coverage.dat"] = coverage_file.read_bytes()
                case_results.append({"testcase": case, "seed": seed, "status": case_status, "exit_code": code})
            order = {"PASS": 0, "FAIL": 1, "ERROR": 2, "TIMEOUT": 3}
            status = max((item["status"] for item in case_results), key=order.__getitem__)
            code = 0 if status == "PASS" else 124 if status == "TIMEOUT" else 1
        else:
            _materialize(project, root)
            mode = ("--cc", "--no-timing", "-Wno-fatal") if tool == "run_compile" else LINT_FLAGS
            result = _run(
                _command(project, executable, project.manifest.top_module, *mode), root, 90,
                commands, outs, errs, tool,
            )
            code = result.exit_code
            status = _status(result, failed="FAIL" if "%Error" in result.stderr or "%Warning" in result.stderr else "ERROR")
    except (OSError, ValueError, KeyError) as error:
        errs.append(str(error))
        status = "ERROR"
    return EDAToolResult(
        tool_name=tool, tool_version=version, input_hash=project.input_hash, status=status, exit_code=code,
        commands=commands, duration_seconds=round(monotonic() - started, 3),
        metrics={"testcases": case_results} if tool in SIMULATION_TOOLS else {},
    ), "\n".join(outs), "\n".join(errs), outputs


def _check_test_sources(project: FrozenProject) -> None:
    for path, text in project.files.items():
        if re.search(r'(?i)\bDPI(?:-C)?\b|verilator\s+(?:systemc|inline_module|hier_block)|\bglobal\s+clocking', text):
            raise ValueError(f"External HDL integrations are unsupported in testbenches: {path}")
        calls = set(re.findall(r"(?<![\w$])\$([A-Za-z_][\w$]*)", text))
        if calls - SAFE_SYSTEM_CALLS:
            raise ValueError(f"Testbench has unsupported system calls: {path}")
        for match in re.finditer(r"\$dumpfile\b", text):
            if not re.match(r'\s*\(\s*"wave\.vcd"\s*\)', text[match.end():]):
                raise ValueError(f"Testbench can only write wave.vcd: {path}")


def _materialize(project: FrozenProject, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in project.files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def _build_command(directory: Path, environment: dict, executable_name: str, *, coverage: bool) -> list[str]:
    # Relative paths work in native Windows workspaces containing non-ASCII directory names.
    shutil.copytree(environment["runtime_include"], directory / "vlt_runtime")
    generated = sorted(path.relative_to(directory).as_posix() for path in (directory / "obj_dir").glob("Vchipflow*.cpp"))
    if not generated:
        raise ValueError("Verilator did not produce any C++ sources")
    runtime = ("verilated.cpp", "verilated_vcd_c.cpp", "verilated_timing.cpp", "verilated_threads.cpp")
    if coverage:
        runtime += ("verilated_cov.cpp",)
    return [
        environment["compiler"], "-std=c++20", "-fcoroutines", "-O0", "-pthread",
        *(["-static"] if os.name == "nt" else []),
        "-Iobj_dir", "-Ivlt_runtime", "-Ivlt_runtime/vltstd",
        "-DVL_TIME_CONTEXT", "-DVM_TIMING=1", "-DVM_TRACE=1", "-DVM_TRACE_VCD=1",
        f"-DVM_COVERAGE={int(coverage)}",
        *generated, *(f"vlt_runtime/{name}" for name in runtime),
        "-o", f"obj_dir/{executable_name}",
    ]


def _command(project: FrozenProject, executable: str, top: str, *mode: str) -> list[str]:
    manifest = project.manifest
    return [
        executable, *mode, "--relative-includes", "--top-module", top,
        *(f"-I{path}" for path in manifest.include_dirs),
        *(f"-D{value}" for value in manifest.defines),
        *manifest.sources,
    ]


def _run(command, directory, timeout, commands, outs, errs, label) -> ProcessResult:
    if timeout <= 0:
        errs.append(f"--- {label} ---\nEDA tool deadline exhausted before launch.")
        return ProcessResult(124, "", "", timed_out=True)
    commands.append(command)
    result = run_process(command, directory, timeout)
    outs.append(f"--- {label} ---\n{result.stdout}")
    errs.append(f"--- {label} ---\n{result.stderr}")
    return result


def _status(result: ProcessResult, *, failed: EDAStatus) -> EDAStatus:
    if result.timed_out:
        return "TIMEOUT"
    if result.resource_exceeded:
        return "ERROR"
    return "PASS" if result.exit_code == 0 else failed
