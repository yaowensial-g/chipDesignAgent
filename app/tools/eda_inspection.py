"""Conservative lexical RTL indexing and bounded scalar/vector VCD inspection."""

import re

from app.tools.project_manifest import FrozenProject, IDENTIFIER


def coverage_summary(data: bytes) -> dict:
    text = data.decode("utf-8", errors="strict")
    if not text.startswith("# SystemC::Coverage-"):
        raise ValueError("Unrecognized Verilator coverage data")
    counts = [
        int(match[1]) for line in text.splitlines()
        if (match := re.fullmatch(r"C '.*' (\d+)", line))
    ]
    if not counts:
        raise ValueError("No coverage counters in the actual report")
    hit = sum(count > 0 for count in counts)
    return {"points": len(counts), "hit": hit, "percentage": round(100 * hit / len(counts), 2)}


def _without_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", lambda match: re.sub(r"[^\n]", " ", match[0]), text, flags=re.S)


def module_hierarchy(project: FrozenProject) -> dict:
    modules = {}
    bodies = {}
    for path, text in project.files.items():
        source = _without_comments(text)
        for match in re.finditer(rf"\bmodule\s+({IDENTIFIER})\b(.*?)\bendmodule\b", source, re.S):
            name = match[1]
            if name in modules:
                raise ValueError(f"Ambiguous lexical module definition: {name}")
            modules[name] = {"path": path, "line": source.count("\n", 0, match.start()) + 1, "instances": []}
            bodies[name] = match[2]
    if project.manifest.top_module not in modules:
        raise ValueError("Top module was not found in the source index")
    for module, body in bodies.items():
        tokens = list(re.finditer(rf"{IDENTIFIER}|[#()\[\],;]", body))
        for index, token in enumerate(tokens):
            if token[0] not in modules or index + 2 >= len(tokens):
                continue
            next_index = index + 1
            if tokens[next_index][0] == "#" and tokens[next_index + 1][0] == "(":
                depth = 1
                next_index += 2
                while next_index < len(tokens) and depth:
                    depth += int(tokens[next_index][0] == "(") - int(tokens[next_index][0] == ")")
                    next_index += 1
            if next_index + 1 < len(tokens) and re.fullmatch(IDENTIFIER, tokens[next_index][0]) and tokens[next_index + 1][0] == "(":
                modules[module]["instances"].append({
                    "module": token[0], "name": tokens[next_index][0],
                    "line": modules[module]["line"] + body.count("\n", 0, token.start()),
                })
    return {"top_module": project.manifest.top_module, "modules": modules, "analysis": "lexical"}


def signal_references(project: FrozenProject, signal: str) -> dict:
    references = []
    word = re.compile(rf"(?<![\w$]){re.escape(signal)}(?![\w$])")
    for path, content in project.files.items():
        for line, text in enumerate(_without_comments(content).splitlines(), 1):
            for match in word.finditer(text):
                role = "reference"
                if re.search(r"\b(?:input|output|inout|logic|wire|reg|bit)\b[^;=]*$", text[:match.start()]):
                    role = "declaration"
                elif re.match(r"\s*(?:\[[^]]+\]\s*)?(?:<=|=(?!=))", text[match.end():]):
                    role = "assignment"
                references.append({"path": path, "line": line, "column": match.start() + 1, "kind": role})
                if len(references) == 2000:
                    return {"signal": signal, "references": references, "truncated": True, "analysis": "lexical"}
    return {"signal": signal, "references": references, "truncated": False, "analysis": "lexical"}


def signal_trace(data: bytes, signal: str, clock: str | None = None, ready: str | None = None) -> dict:
    scopes: list[str] = []
    ids: dict[str, str] = {}
    lines = iter(data.decode("utf-8", errors="strict").splitlines())
    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "$scope" and len(tokens) >= 4:
            scopes.append(tokens[2])
        elif tokens[0] == "$upscope" and scopes:
            scopes.pop()
        elif tokens[0] == "$var" and len(tokens) >= 6:
            ids[".".join([*scopes, tokens[4]])] = tokens[3]
        elif tokens[0] == "$enddefinitions":
            break
    requested = [item for item in (signal, clock, ready) if item]
    selected = {}
    for name in requested:
        matches = [value for key, value in ids.items() if key == name or ("." not in name and key.rsplit(".", 1)[-1] == name)]
        if len(set(matches)) != 1:
            raise ValueError(f"VCD signal is missing or ambiguous: {name}")
        selected[name] = matches[0]
    values: dict[str, str] = {}
    changes = []
    time = 0
    last_clock = None
    cycles = handshakes = unknown_cycles = 0

    def sample():
        nonlocal cycles, handshakes, unknown_cycles, last_clock
        if clock:
            level = values.get(selected[clock])
            if level == "1" and last_clock == "0":
                cycles += 1
                value = values.get(selected[signal], "x")
                peer = values.get(selected[ready], "x") if ready else "1"
                handshakes += int(value == peer == "1")
                unknown_cycles += int(any(bit in value + peer for bit in "xz"))
            last_clock = level

    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            sample()
            time = int(line[1:])
        elif line and line[0] in "01xXzZbB":
            if line[0] in "bB":
                parts = line[1:].split()
                if len(parts) != 2:
                    continue
                value, code = parts
            else:
                value, code = line[0], line[1:].strip()
            values[code] = value.lower()
            if code == selected[signal] and len(changes) < 2000:
                changes.append({"tick": time, "value": value.lower()})
    sample()
    return {"signal": signal, "clock": clock, "ready": ready, "changes": changes, "clock_edges": cycles,
            "handshakes": handshakes if ready else None, "unknown_cycles": unknown_cycles}
