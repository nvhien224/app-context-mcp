from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import AppIndex, ExecutionFlow, ImportEdge, SymbolEdge, SymbolNode

logger = __import__("logging").getLogger(__name__)

# ── Regex for import/part parsing ─────────────────────────────────────────
_IMPORT_RE = re.compile(
    r"^(?:import|export)\s+(?:'|\"|\`)([^'\"`]+)(?:'|\"|\`)(?:\s*as\s+\w+)?(?:\s*show\s+([^;]+))?(?:\s*hide\s+([^;]+))?;?$",
    re.MULTILINE,
)
_PART_RE = re.compile(r"^part\s+(?:'|\"|\`)([^'\"`]+)(?:'|\"|\`);?", re.MULTILINE)

# Method invocation patterns
_METHOD_INVOCATION_RE = re.compile(
    r"\b(\w+)\.(\w+)\s*\([^)]*\)",
)

# BLoC add pattern: context.read<SomeBloc>().add(SomeEvent()) or _bloc.add(...)
_BLOC_ADD_RE = re.compile(
    r"\b(\w+)\.add\s*\(\s*(\w+)\s*\)",
)

# Provider read pattern
_CONTEXT_READ_RE = re.compile(
    r"context\.read\s*\(?\s*<?\s*(\w+)\s*>?\s*\)?",
)


def _add_evidence(index: AppIndex, source_type: str, file: Path, symbol: str, line: int, snippet: str, why: str) -> str:
    eid = f"ev_{len(index.evidence) + 1:04d}"
    rel = str(file.relative_to(index.repo_path))
    index.evidence[eid] = __import__("app_context_mcp.models", fromlist=["Evidence"]).Evidence(
        id=eid,
        source_type=source_type,
        file=rel,
        symbol=symbol,
        lines=str(line),
        snippet=snippet,
        why_relevant=why,
    )
    return eid


# ── Import resolution ─────────────────────────────────────────────────────

def resolve_import(import_path: str, source_file: Path, repo: Path) -> Path | None:
    """Resolve a Dart import path to an actual file."""
    if import_path.startswith("dart:"):
        return None  # SDK import
    if import_path.startswith("package:"):
        # package:foo/bar.dart → try lib/bar.dart in repo
        rest = import_path.removeprefix("package:")
        if "/" in rest:
            pkg, sub = rest.split("/", 1)
        else:
            pkg, sub = rest, ""
        # Try repo root / lib / sub
        candidates = [
            repo / "lib" / sub,
            repo / sub,
        ]
        for c in candidates:
            if c.exists():
                return c
        return None
    # Relative import
    if import_path.startswith("./") or import_path.startswith("../"):
        resolved = (source_file.parent / import_path).resolve()
        if resolved.exists():
            return resolved
    # Bare file name or path relative to lib/
    candidates = [
        source_file.parent / import_path,
        repo / "lib" / import_path,
        repo / import_path,
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def extract_imports(text: str, file: Path, repo: Path, index: AppIndex) -> None:
    """Parse imports from Dart source and populate index.imports + import_graph."""
    rel = str(file.relative_to(repo))
    index.import_graph.setdefault(rel, set())

    for match in _IMPORT_RE.finditer(text):
        path = match.group(1)
        line = text[: match.start()].count("\n") + 1
        is_pkg = path.startswith("package:")
        resolved = resolve_import(path, file, repo)
        if resolved:
            target_rel = str(resolved.relative_to(repo))
            edge = ImportEdge(
                source_file=rel,
                target_file=target_rel,
                import_path=path,
                is_package=is_pkg,
                line=line,
            )
            index.imports.append(edge)
            index.import_graph[rel].add(target_rel)

    # Part directives
    for match in _PART_RE.finditer(text):
        path = match.group(1)
        line = text[: match.start()].count("\n") + 1
        resolved = resolve_import(path, file, repo)
        if resolved:
            target_rel = str(resolved.relative_to(repo))
            edge = ImportEdge(
                source_file=rel,
                target_file=target_rel,
                import_path=path,
                is_package=False,
                line=line,
            )
            index.imports.append(edge)
            index.import_graph[rel].add(target_rel)


# ── Symbol extraction (lightweight regex) ────────────────────────────────

def extract_symbols(text: str, file: Path, repo: Path, index: AppIndex) -> None:
    """Extract class/method/enum declarations as SymbolNodes."""
    rel = str(file.relative_to(repo))
    lines = text.splitlines()

    # Classes
    for m in re.finditer(r"^\s*class\s+(\w+)", text, re.MULTILINE):
        name = m.group(1)
        line = text[: m.start()].count("\n") + 1
        sid = f"sym:cls:{rel}:{name}"
        if sid not in index.symbols:
            index.symbols[sid] = SymbolNode(
                id=sid,
                kind="class",
                name=name,
                file=rel,
                line=line,
            )

    # Methods (simple signature)
    for m in re.finditer(r"(?:Future<[^>]+>|Future<void>|[\w<>?,\s]+)\s+(\w+)\s*\([^)]*\)\s*(?:async\s*)?\{", text):
        name = m.group(1)
        line = text[: m.start()].count("\n") + 1
        # Find enclosing class (nearest class before this method)
        enclosing = ""
        cls_match = None
        for cls_m in re.finditer(r"class\s+(\w+)", text[: m.start()]):
            cls_match = cls_m
        if cls_match:
            enclosing = cls_match.group(1)
        sid = f"sym:method:{rel}:{enclosing}.{name}" if enclosing else f"sym:method:{rel}:{name}"
        if sid not in index.symbols:
            index.symbols[sid] = SymbolNode(
                id=sid,
                kind="method",
                name=name,
                file=rel,
                line=line,
                enclosing_class=enclosing or None,
                signature=lines[line - 1].strip() if line <= len(lines) else "",
            )

    # Enums
    for m in re.finditer(r"^\s*enum\s+(\w+)", text, re.MULTILINE):
        name = m.group(1)
        line = text[: m.start()].count("\n") + 1
        sid = f"sym:enum:{rel}:{name}"
        if sid not in index.symbols:
            index.symbols[sid] = SymbolNode(
                id=sid,
                kind="enum",
                name=name,
                file=rel,
                line=line,
            )


# ── Cross-file method-call edges (intra + inter file) ────────────────────

def extract_method_calls(text: str, file: Path, repo: Path, index: AppIndex) -> None:
    """Detect obj.method() calls and link to known symbols."""
    rel = str(file.relative_to(repo))
    lines = text.splitlines()

    for m in _METHOD_INVOCATION_RE.finditer(text):
        obj = m.group(1)
        method = m.group(2)
        line = text[: m.start()].count("\n") + 1
        snippet = lines[line - 1].strip() if line <= len(lines) else ""

        # Try to find target symbol in same file first
        target_sid = _find_method_symbol(obj, method, rel, index)
        if target_sid:
            edge = SymbolEdge(
                source=f"sym:method:{rel}:?",  # caller unknown at this stage
                target=target_sid,
                kind="calls",
                file=rel,
                line=line,
            )
            index.symbol_edges.append(edge)
        else:
            # Try imported files
            for imported_rel in index.import_graph.get(rel, set()):
                target_sid = _find_method_symbol(obj, method, imported_rel, index)
                if target_sid:
                    edge = SymbolEdge(
                        source=f"sym:method:{rel}:?",
                        target=target_sid,
                        kind="calls",
                        file=rel,
                        line=line,
                    )
                    index.symbol_edges.append(edge)
                    break


def _find_method_symbol(obj_name: str, method_name: str, file_rel: str, index: AppIndex) -> str | None:
    """Find a symbol matching obj.method in a given file."""
    # Direct match: class.method
    sid = f"sym:method:{file_rel}:{obj_name}.{method_name}"
    if sid in index.symbols:
        return sid
    # Try class name derived from variable name (e.g., _orderRepo → OrderRepository)
    # Simple heuristic: strip prefix/suffix
    guessed = _guess_class_name(obj_name)
    for g in guessed:
        sid = f"sym:method:{file_rel}:{g}.{method_name}"
        if sid in index.symbols:
            return sid
    return None


def _guess_class_name(var_name: str) -> list[str]:
    """Heuristic: _orderRepo → OrderRepository, bloc → SomeBloc."""
    guesses: list[str] = []
    # Strip leading underscore
    base = var_name.lstrip("_")
    # CamelCase from snake_case
    parts = base.split("_")
    camel = "".join(p.capitalize() for p in parts)
    guesses.append(camel)
    # Common suffixes
    for suffix in ["Repository", "Repo", "Bloc", "Cubit", "Provider", "Service", "Client", "DataSource"]:
        if not base.endswith(suffix):
            guesses.append(camel + suffix)
    # If already ends with suffix, try without
    for suffix in ["Repository", "Repo", "Bloc", "Cubit"]:
        if base.endswith(suffix):
            guesses.append(camel[: -len(suffix)])
    return guesses


# ── Execution Flow builder ────────────────────────────────────────────────

def build_execution_flows(index: AppIndex) -> None:
    """Trace end-to-end flows: Screen → BLoC/Controller → Repository → API."""
    flows: list[ExecutionFlow] = []
    flow_id = 0

    # For each screen, look for method calls in same file + imported files
    for screen_name, screen_info in index.screens.items():
        screen_file = screen_info.file
        screen_text = (index.repo_path / screen_file).read_text(encoding="utf-8", errors="ignore")
        screen_lines = screen_text.splitlines()

        # Find onPressed / onTap handlers
        handler_pattern = re.compile(r"on(?:Pressed|Tap|Changed|Submitted):\s*(?:\(\)\s*=>\s*)?(\w+\.\w+\s*\([^)]*\)|\w+\s*\([^)]*\)|\(\)\s*=>\s*\{[^}]*\})")
        bloc_adds: list[tuple[str, str, int]] = []  # (bloc_var, event_name, line)
        repo_calls: list[tuple[str, str, int]] = []  # (repo_var, method_name, line)
        api_calls_local: list[tuple[str, str, int]] = []  # (method, path, line)

        # Parse same-file
        for m in _BLOC_ADD_RE.finditer(screen_text):
            bloc_var, event_name = m.groups()
            line = screen_text[: m.start()].count("\n") + 1
            bloc_adds.append((bloc_var, event_name, line))

        for m in _METHOD_INVOCATION_RE.finditer(screen_text):
            obj, method = m.groups()
            line = screen_text[: m.start()].count("\n") + 1
            # Detect repository pattern
            if any(obj.endswith(s) for s in ("Repo", "Repository", "repo", "_repo")):
                repo_calls.append((obj, method, line))

        # Also scan imported files for BLoC/Repo definitions
        for imported_rel in index.import_graph.get(screen_file, set()):
            imported_path = index.repo_path / imported_rel
            if not imported_path.exists():
                continue
            imported_text = imported_path.read_text(encoding="utf-8", errors="ignore")
            # If imported file defines a Bloc/Cubit/ViewModel class → link
            for m in re.finditer(r"class\s+(\w+)(?:Bloc|Cubit|ViewModel|Controller)", imported_text):
                bloc_class = m.group(1)
                # Check if screen references this bloc
                if bloc_class in screen_text or bloc_class.lower() in screen_text.lower():
                    # Find add calls
                    for bm in _BLOC_ADD_RE.finditer(screen_text):
                        bv, ev = bm.groups()
                        line = screen_text[: bm.start()].count("\n") + 1
                        bloc_adds.append((bv, ev, line))

        # Build flow steps
        has_flow = bool(bloc_adds or repo_calls or index.hooks)
        if has_flow:
            steps: list[dict] = []
            screens_involved = [screen_name]
            apis = []
            fields = []
            ev_ids: list[str] = []

            steps.append({
                "node": screen_name,
                "kind": "screen",
                "file": screen_file,
                "line": 1,
            })

            # Widget steps
            for wid, w in index.widgets.items():
                if w.enclosing_screen == screen_name:
                    steps.append({
                        "node": w.widget_type + (f" ({w.text})" if w.text else ""),
                        "kind": "ui_widget",
                        "file": w.file,
                        "line": w.line,
                        "condition": w.condition_expression,
                    })

            # Hook steps from indexed hooks
            for h in index.hooks:
                if h.enclosing_screen == screen_name:
                    steps.append({
                        "node": f"{h.hook_type}: {h.handler_method}",
                        "kind": "ui_hook",
                        "file": h.file,
                        "line": h.line,
                    })
                    # Trace hook → cubit/bloc → repo → api
                    handler = h.handler_method
                    # Check if handler is cubit.method() pattern
                    if '.' in handler:
                        var_name, method_name = handler.split('.', 1)
                        method_name = method_name.split('(')[0]  # strip args
                        # Find file for bloc/cubit
                        bloc_file_rel = _find_file_for_bloc_var(var_name, screen_file, index)
                        if bloc_file_rel:
                            # Trace API calls where client_method matches handler method name
                            for api in index.api_calls:
                                if api.file == bloc_file_rel and api.client_method == method_name:
                                    steps.append({
                                        "node": f"{api.method} {api.path_template}",
                                        "kind": "api_call",
                                        "file": api.file,
                                        "line": api.line,
                                    })
                                    apis.append(api.path_template)
                                    ev_ids.append(api.evidence_id)
                            # If no direct API in bloc file, trace through imported files (e.g. order_api.dart)
                            if bloc_file_rel in index.import_graph:
                                for imported_api_file in index.import_graph[bloc_file_rel]:
                                    for api in index.api_calls:
                                        if api.file == imported_api_file and api.client_method == method_name:
                                            steps.append({
                                                "node": f"{api.method} {api.path_template}",
                                                "kind": "api_call",
                                                "file": api.file,
                                                "line": api.line,
                                            })
                                            apis.append(api.path_template)
                                            ev_ids.append(api.evidence_id)

            # Bloc step (legacy .add pattern)
            for bv, ev, ln in bloc_adds:
                steps.append({
                    "node": f"{bv}.add({ev})",
                    "kind": "bloc_add",
                    "file": screen_file,
                    "line": ln,
                })
                # Trace bloc → repo → api via imported bloc file
                bloc_file_rel = _find_file_for_bloc_var(bv, screen_file, index)
                if bloc_file_rel:
                    bloc_text = (index.repo_path / bloc_file_rel).read_text(encoding="utf-8", errors="ignore")
                    for rm in _METHOD_INVOCATION_RE.finditer(bloc_text):
                        obj, method = rm.groups()
                        if any(obj.endswith(s) for s in ("Repo", "Repository", "repo", "_repo")):
                            rln = bloc_text[: rm.start()].count("\n") + 1
                            steps.append({
                                "node": f"{obj}.{method}()",
                                "kind": "repo_call",
                                "file": bloc_file_rel,
                                "line": rln,
                            })
                            # Link to API
                            for api in index.api_calls:
                                if api.file == bloc_file_rel and api.client_method == method:
                                    steps.append({
                                        "node": f"{api.method} {api.path_template}",
                                        "kind": "api_call",
                                        "file": api.file,
                                        "line": api.line,
                                    })
                                    apis.append(api.path_template)
                                    ev_ids.append(api.evidence_id)

            # Direct repo calls in screen → API linkage
            for rv, rm, rln in repo_calls:
                steps.append({
                    "node": f"{rv}.{rm}()",
                    "kind": "repo_call",
                    "file": screen_file,
                    "line": rln,
                })
                for api in index.api_calls:
                    if api.file == screen_file and api.client_method == rm:
                        steps.append({
                            "node": f"{api.method} {api.path_template}",
                            "kind": "api_call",
                            "file": api.file,
                            "line": api.line,
                        })
                        apis.append(api.path_template)
                        ev_ids.append(api.evidence_id)

            if len(steps) > 1:
                flow_id += 1
                flow = ExecutionFlow(
                    name=f"flow_{flow_id:03d}_{screen_name}",
                    start_node=screen_name,
                    end_node=steps[-1]["node"],
                    steps=steps,
                    screens_involved=screens_involved,
                    apis_involved=apis,
                    fields_involved=fields,
                    evidence_ids=ev_ids,
                )
                flows.append(flow)

    index.execution_flows = flows


def _find_file_for_bloc_var(var_name: str, screen_file: str, index: AppIndex) -> str | None:
    """Guess which imported file contains the BLoC/Cubit referenced by var_name."""
    import_rels = index.import_graph.get(screen_file, set())

    # Heuristic A: import file name contains bloc/cubit/viewmodel/controller
    for imported_rel in import_rels:
        lower = imported_rel.lower()
        if any(k in lower for k in ("cubit", "bloc", "viewmodel", "controller", "view_model")):
            path = index.repo_path / imported_rel
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="ignore")
                if re.search(r"class\s+\w+(?:Cubit|Bloc|ViewModel|Controller|View_Model)\b", text):
                    return imported_rel

    # Heuristic B: guess class name from var
    guesses = _guess_class_name(var_name)
    for imported_rel in import_rels:
        imported_path = index.repo_path / imported_rel
        if not imported_path.exists():
            continue
        text = imported_path.read_text(encoding="utf-8", errors="ignore")
        for g in guesses:
            if f"class {g}" in text or f"class {g}Bloc" in text or f"class {g}Cubit" in text:
                return imported_rel
    return None


# ── Public builder ─────────────────────────────────────────────────────────

from .indexer import _should_ignore

def build_graph(index: AppIndex) -> None:
    """Build import graph, symbols, cross-file edges, and execution flows."""
    dart_files = [p for p in index.repo_path.rglob("*.dart") if not _should_ignore(p)]
    # Pass 1: imports + symbols per file
    for file in dart_files:
        text = file.read_text(encoding="utf-8", errors="ignore")
        extract_imports(text, file, index.repo_path, index)
        extract_symbols(text, file, index.repo_path, index)

    # Pass 2: cross-file method calls (needs symbols + imports ready)
    for file in dart_files:
        text = file.read_text(encoding="utf-8", errors="ignore")
        extract_method_calls(text, file, index.repo_path, index)

    # Pass 3: execution flows (needs everything)
    build_execution_flows(index)

    # Reverse import graph (who imports me)
    reverse: dict[str, set[str]] = {}
    for src, targets in index.import_graph.items():
        for tgt in targets:
            reverse.setdefault(tgt, set()).add(src)
    index.import_graph_reverse = reverse
