from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .graph_builder import build_graph
from .models import ApiCall, AppIndex, ConditionInfo, Evidence, FieldInfo, ScreenInfo

IGNORE_DIRS = {
    ".git", ".dart_tool", "build", "ios/Pods", "android/.gradle", ".gradle",
    "node_modules", ".venv", "venv",
}
SECRET_PATTERNS = (".env", ".pem", ".key", "secret", "secrets")


CLASS_RE = re.compile(r"class\s+(\w+)")
ROUTE_RE = re.compile(r"(?:static\s+const\s+\w+\s*=\s*)['\"]([^'\"]*/[^'\"]*)['\"]")
TEXT_RE = re.compile(r"(?:Text|title:\s*const\s*Text)\(\s*['\"]([^'\"]{2,})['\"]")
CLIENT_RE = re.compile(r"client\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")
METHOD_RE = re.compile(r"(?:Future<[^>]+>|Future<void>|[\w<>?,\s]+)\s+(\w+)\s*\([^)]*\)\s*(?:async\s*)?{")
JSON_FIELD_RE = re.compile(r"(\w+)\s*:\s*(?:[\w.]+\([^\n]*?)?json\[['\"]([^'\"]+)['\"]\](?:\s+as\s+([\w<>?]+))?")
VISIBILITY_RE = re.compile(r"(?:Visibility\s*\([\s\S]{0,200}?visible:\s*|enabled:\s*|onPressed:\s*)([^,\n]+)")
IDENT_RE = re.compile(r"\b(?:order|state|model|item)\.(\w+)\b|\b(can\w+|is\w+|status|paymentStatus|deliveryStatus)\b")


def should_ignore(path: Path) -> bool:
    text = str(path)
    parts = set(path.parts)
    if any(part in parts for part in IGNORE_DIRS):
        return True
    lower = path.name.lower()
    return any(pat in lower for pat in SECRET_PATTERNS)


def normalize_path_template(path: str) -> str:
    # Dart string interpolation examples: /orders/$id, /orders/${id}/cancel.
    path = re.sub(r"\$\{?\w+\}?", "{id}", path)
    return path


def line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def line_snippet(lines: list[str], number: int, radius: int = 1) -> str:
    start = max(1, number - radius)
    end = min(len(lines), number + radius)
    return "\n".join(lines[start - 1:end]).strip()


def add_evidence(index: AppIndex, source_type: str, file: Path, symbol: str, line: int, snippet: str, why: str) -> str:
    eid = f"ev_{len(index.evidence) + 1:04d}"
    rel = str(file.relative_to(index.repo_path))
    index.evidence[eid] = Evidence(
        id=eid,
        source_type=source_type,
        file=rel,
        symbol=symbol,
        lines=str(line),
        snippet=snippet,
        why_relevant=why,
    )
    return eid


def build_index(repo_path: str | Path, semble_candidate_files: list[str] | None = None) -> AppIndex:
    repo = Path(repo_path).resolve()
    index = AppIndex(repo_path=repo)
    dart_files = [p for p in repo.rglob("*.dart") if not should_ignore(p)]
    
    # Apply Semble candidate pre-filter if available (fast / scoped)
    if semble_candidate_files:
        wanted = set(semble_candidate_files)
        dart_files = [p for p in dart_files if str(p.relative_to(repo)) in wanted or p.name in wanted]
    
    index.dart_files = len(dart_files)

    for file in dart_files:
        text = file.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        classes = CLASS_RE.findall(text)
        current_class = classes[0] if classes else file.stem

        if any(name.endswith(("Screen", "Page", "View")) for name in classes):
            screen_name = next(name for name in classes if name.endswith(("Screen", "Page", "View")))
            route = None
            route_match = ROUTE_RE.search(text)
            if route_match:
                route = route_match.group(1)
            visible_texts = TEXT_RE.findall(text)
            ev_id = add_evidence(index, "code", file, screen_name, 1, line_snippet(lines, 1, 5), "Declares Flutter screen/page/view")
            index.screens[screen_name] = ScreenInfo(
                name=screen_name,
                file=str(file.relative_to(repo)),
                route=route,
                visible_texts=visible_texts,
                evidence_ids=[ev_id],
            )

        method_spans: list[tuple[int, str]] = []
        for match in METHOD_RE.finditer(text):
            method_spans.append((match.start(), match.group(1)))

        for match in CLIENT_RE.finditer(text):
            method = match.group(1).upper()
            path_template = normalize_path_template(match.group(2))
            ln = line_no(text, match.start())
            client_method = _nearest_method(method_spans, match.start()) or current_class
            ev_id = add_evidence(index, "code", file, client_method, ln, line_snippet(lines, ln), "Client API call")
            index.api_calls.append(ApiCall(method, path_template, client_method, str(file.relative_to(repo)), ln, ev_id))

        for match in JSON_FIELD_RE.finditer(text):
            field_name, raw_name, type_hint = match.groups()
            ln = line_no(text, match.start())
            ev_id = add_evidence(index, "code", file, f"{current_class}.{field_name}", ln, line_snippet(lines, ln), "Maps JSON/API field into Dart model")
            info = FieldInfo(current_class, field_name, raw_name, type_hint, str(file.relative_to(repo)), ln, ev_id)
            index.fields[info.qualified_name] = info

        for match in VISIBILITY_RE.finditer(text):
            expr = match.group(1).strip()
            if "=>" in expr or expr in {"null", "()"}:
                continue
            ln = line_no(text, match.start())
            fields = sorted({g for tup in IDENT_RE.findall(expr) for g in tup if g})
            if not fields and not any(token in expr for token in ("==", "!=", "&&", "||", "can", "status")):
                continue
            target = _nearby_widget_target(text, match.start())
            ev_id = add_evidence(index, "code", file, target, ln, line_snippet(lines, ln, 2), "UI condition/handler expression")
            index.conditions.append(ConditionInfo(expr, target, str(file.relative_to(repo)), ln, fields, ev_id))

    # ── GitNexus-inspired: import graph + execution flows ──
    build_graph(index, dart_files)

    return index


def _nearest_method(method_spans: list[tuple[int, str]], offset: int) -> str | None:
    before = [name for start, name in method_spans if start <= offset]
    return before[-1] if before else None


def _nearby_widget_target(text: str, offset: int) -> str:
    window = text[offset: offset + 500]
    button_text = TEXT_RE.search(window)
    if button_text:
        return f"UI element text: {button_text.group(1)}"
    return "UI condition"


def git_info(repo_path: str | Path) -> dict:
    repo = Path(repo_path)
    def run(args: list[str]) -> str | None:
        try:
            return subprocess.check_output(args, cwd=repo, stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            return None
    commit = run(["git", "rev-parse", "HEAD"])
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    dirty = bool(run(["git", "status", "--porcelain"]))
    return {"git_commit": commit, "git_branch": branch, "dirty": dirty}
