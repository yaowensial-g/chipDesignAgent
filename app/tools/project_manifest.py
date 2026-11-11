"""Read and freeze an explicitly configured HDL project within one Workspace."""

import hashlib
import re
import shlex
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from app.services.workspace_service import WorkspaceService


IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$]*"
PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
DEFINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_.'+-]+)?")
HDL = {".v", ".sv", ".vh", ".svh"}
MAX_FILES = 96
MAX_SOURCE_BYTES = 2_000_000


class Testcase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    top_module: str = Field(pattern=f"^{IDENTIFIER}$")
    sources: list[str] = Field(min_length=1, max_length=24)
    depends_on: list[str] = Field(default_factory=list, max_length=96)


class ProjectManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    top_module: str = Field(pattern=f"^{IDENTIFIER}$")
    sources: list[str] = Field(default_factory=list, max_length=MAX_FILES)
    filelists: list[str] = Field(default_factory=list, max_length=16)
    include_dirs: list[str] = Field(default_factory=list, max_length=16)
    defines: list[str] = Field(default_factory=list, max_length=64)
    testcases: dict[str, Testcase] = Field(default_factory=dict, max_length=16)


class FrozenProject(BaseModel):
    manifest: ProjectManifest
    files: dict[str, str]
    hashes: dict[str, str]
    manifest_path: str
    input_hash: str


def relative_path(name: str) -> str:
    if not PATH.fullmatch(name) or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("Project paths must be normalized Workspace-relative paths")
    return name


def read_project(workspaces: WorkspaceService, workspace_id: str, project_id: str, manifest_path: str) -> FrozenProject:
    workspace = workspaces.get(workspace_id)
    if workspace is None or workspace.project_id != project_id:
        raise ValueError("Project Workspace is unavailable")
    root = workspaces.resolve_path(workspace_id, ".")

    def resolve(name: str) -> Path:
        target = workspaces.resolve_path(workspace_id, relative_path(name))
        if target != root / name:
            raise ValueError("Project inputs cannot use symbolic links")
        return target

    def read(name: str, size: int) -> bytes:
        path = resolve(name)
        if not path.is_file():
            raise ValueError(f"Project file is missing: {name}")
        with path.open("rb") as source:
            data = source.read(size + 1)
        if not data or len(data) > size or b"\0" in data:
            raise ValueError(f"Invalid text input: {name}")
        return data

    raw_manifest = read(manifest_path, 64_000)
    manifest = ProjectManifest.model_validate_json(raw_manifest)
    sources = list(manifest.sources)
    dirs = list(manifest.include_dirs)
    defines = list(manifest.defines)
    filelists: dict[str, str] = {}
    for name in manifest.filelists:
        text = read(name, 64_000).decode("utf-8")
        filelists[name] = text
        for line in text.splitlines():
            for token in shlex.split(line.split("//", 1)[0], comments=True, posix=True):
                if token.startswith("+incdir+"):
                    dirs.extend(token.removeprefix("+incdir+").split("+"))
                elif token.startswith("+define+"):
                    defines.append(token.removeprefix("+define+"))
                elif PurePosixPath(relative_path(token)).suffix.lower() in HDL:
                    sources.append(token)
                else:
                    raise ValueError(f"Unsupported Filelist entry in {name}")
    sources = list(dict.fromkeys(sources))
    dirs = list(dict.fromkeys(dirs))
    defines = list(dict.fromkeys(defines))
    if not sources or len(dirs) > 16 or len(defines) > 64 or any(not DEFINE.fullmatch(item) for item in defines):
        raise ValueError("Invalid project sources, include directories or macro definitions")
    for name in manifest.testcases:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", name):
            raise ValueError("Invalid testcase ID")

    names = dict.fromkeys([*sources, *(name for case in manifest.testcases.values() for name in case.sources)])
    for name in dirs:
        directory = resolve(name)
        if not directory.is_dir():
            raise ValueError(f"Include directory is missing: {name}")
        # Includes use explicit paths relative to a declared directory or to a source file.
        for child in directory.iterdir():
            if child.suffix.lower() in HDL:
                names[child.relative_to(root).as_posix()] = None
    if len(names) > MAX_FILES:
        raise ValueError("Project exceeds the source file limit")

    files: dict[str, str] = {}
    hashes = {manifest_path: hashlib.sha256(raw_manifest).hexdigest()}
    hashes.update({name: hashlib.sha256(text.encode()).hexdigest() for name, text in filelists.items()})
    total = 0
    for name in names:
        if PurePosixPath(relative_path(name)).suffix.lower() not in HDL:
            raise ValueError(f"Unsupported HDL extension: {name}")
        content = workspaces.read_rtl_source(workspace_id, project_id, name, byte_limit=256_000)
        total += len(content)
        if total > MAX_SOURCE_BYTES:
            raise ValueError("Project exceeds the source byte limit")
        files[name] = content.decode("utf-8")
        hashes[name] = hashlib.sha256(content).hexdigest()
    for name, text in files.items():
        _check_includes(name, text, files, dirs)
    if any(path not in files for case in manifest.testcases.values() for path in case.depends_on):
        raise ValueError("Testcase dependency must refer to a frozen HDL file")
    manifest = manifest.model_copy(update={"sources": sources, "include_dirs": dirs, "defines": defines})
    fingerprint = "\n".join(f"{key}\0{digest}" for key, digest in sorted(hashes.items()))
    return FrozenProject(
        manifest=manifest, files=files, hashes=hashes, manifest_path=manifest_path,
        input_hash=hashlib.sha256(fingerprint.encode()).hexdigest(),
    )


def _check_includes(name: str, text: str, files: dict[str, str], dirs: list[str]) -> None:
    if re.search(r"`(?:uselib|verilator_config)\b|``|\\[\r\n]", text):
        raise ValueError(f"Unsupported preprocessor directive: {name}")
    for match in re.finditer(r'`include\b([^\n]*)', text):
        literal = re.fullmatch(r'\s*"([^"]+)"\s*(?://.*)?', match[1])
        if literal is None:
            raise ValueError(f"Include must be a fixed project path: {name}")
        include = relative_path(literal[1])
        parents = [PurePosixPath(name).parent, *(PurePosixPath(path) for path in dirs)]
        if not any((parent / include).as_posix() in files for parent in parents):
            raise ValueError(f"Include is not in the frozen project: {include}")
