"""Fast incremental indexer for Flutter/Dart codebases.Uses SQLite for incremental parsing."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app_context_mcp.models import (
    AppIndex, ApiCall, ConditionInfo, Evidence, FieldInfo, ScreenInfo,
)

CLASS_RE = re.compile(r"class\s+(\w+)")
ROUTE_RE = re.compile(r"static\s+const\s+(?:String\s+)?\w*[rR]oute\w*\s*=\s*['\"]([^'\"]+)['\"]")
TEXT_RE = re.compile(r"Text\s*\(\s*['\"]([^'\"]{1,200})['\"]\s*\)")
CLIENT_RE = re.compile(r"client\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")
METHOD_RE = re.compile(r"(?:Future<[^>]+>|Future<void>|[\w<>?,\s]+)\s+(\w+)\s*\([^)]*\)\s*(?:async\s*)?\{")
VISIBILITY_RE = re.compile(r"(?:enabled|visible)\s*:\s*([^,\)\}]+)")
# Flutter framework patterns (Dio, hooks, Riverpod, AutoRoute, localization)
DIO_RE = re.compile(r"dio\.(get|post|put|delete|patch)\("+r"\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
HOOK_RE = re.compile(r"(useQuery|useMutation|useFuture|useMemoized)\s*\(")
RIVERPOD_RE = re.compile(r"ref\.(read|watch|listen)\s*\(")
AUTOROUTE_RE = re.compile(r"(@RoutePage)\s*\(")
TR_RE = re.compile(r'([\'\"].*?[\'\"])\.tr\b', re.IGNORECASE)
FREEZED_RE = re.compile(r"(@freezed)\b")
JSON_SERIAL_RE = re.compile(r"(@JsonSerializable)\b")
IDENT_RE = re.compile(r"\b\w+\b")

def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:32]

def _line_no(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1

def _snippet(text: str, offset: int, radius: int = 1) -> str:
    lines = text.splitlines()
    ln = _line_no(text, offset)
    start = max(0, ln - radius - 1)
    end = min(len(lines), ln + radius)
    return "\n".join(lines[start:end]).strip()

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = memory")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS file_cache (rel_path TEXT PRIMARY KEY, mtime REAL, size INTEGER, parsed_json TEXT);
    CREATE INDEX IF NOT EXISTS idx_fc_mtime ON file_cache(mtime);
    CREATE TABLE IF NOT EXISTS repo_meta (repo_path TEXT PRIMARY KEY, last_scan_ts TEXT, dart_files INTEGER DEFAULT 0);
    """)

def normalize_path_template(path: str) -> str:
    return re.sub(r"\$\{?\w+\}?", "{id}", path)

def _parse_single_file(file: Path, repo: Path) -> dict[str, Any]:
    text = file.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    classes = CLASS_RE.findall(text)
    current_class = classes[0] if classes else file.stem
    rel = str(file.relative_to(repo))

    parsed: dict[str, Any] = {"screens": [], "api_calls": [], "fields": [], "conditions": [], "evidence": [], "hooks": [], "riverpod_reads": [], "annotations": [], "localizations": [], "model_annotations": []}
    ev_counter = [0]

    def add_ev(source_type: str, symbol: str, line: int, snippet_text: str, why: str) -> str:
        ev_counter[0] += 1
        eid = f"ev_{ev_counter[0]:04d}"
        parsed["evidence"].append({"id": eid, "source_type": source_type, "symbol": symbol, "line": line, "snippet": snippet_text, "why": why})
        return eid

    # Screens
    for name in classes:
        if name.endswith(("Screen", "Page", "View")):
            route = None
            rm = ROUTE_RE.search(text)
            if rm:
                route = rm.group(1)
            vtexts = TEXT_RE.findall(text)
            ev_id = add_ev("code", name, 1, _snippet(text, 0, 5), "Declares Flutter screen/page/view")
            parsed["screens"].append({"name": name, "route": route, "visible_texts": vtexts, "evidence_id": ev_id})
            break

    # Method positions for nearest lookup
    method_spans: list[tuple[int, str]] = []
    for m in METHOD_RE.finditer(text):
        method_spans.append((m.start(), m.group(1)))

    # API calls (client.*)
    for m in CLIENT_RE.finditer(text):
        method = m.group(1).upper()
        path_tmpl = normalize_path_template(m.group(2))
        ln = _line_no(text, m.start())
        client_method = _nearest(method_spans, m.start()) or current_class
        parsed["api_calls"].append({"method": method, "path": path_tmpl, "client_method": client_method, "line": ln, "evidence_id": add_ev("code", client_method, ln, _snippet(text, m.start()), "Client API call")})

    # Dio calls
    for m in DIO_RE.finditer(text):
        method = m.group(1).upper()
        path_tmpl = normalize_path_template(m.group(2))
        ln = _line_no(text, m.start())
        caller = _nearest(method_spans, m.start()) or current_class
        parsed["api_calls"].append({"method": method, "path": path_tmpl, "client_method": caller, "line": ln, "evidence_id": add_ev("code", caller, ln, _snippet(text, m.start()), "Dio API call")})

    # Hooks
    for m in HOOK_RE.finditer(text):
        hook_name = m.group(1)
        ln = _line_no(text, m.start())
        parsed["hooks"].append({"name": hook_name, "line": ln, "file": rel,
                                 "evidence_id": add_ev("code", hook_name, ln, _snippet(text, m.start()), f"Flutter hook: {hook_name}")})

    # Riverpod ref.read/watch/listen
    for m in RIVERPOD_RE.finditer(text):
        access_type = m.group(1)
        ln = _line_no(text, m.start())
        parsed["riverpod_reads"].append({"access_type": access_type, "line": ln, "file": rel,
                                         "evidence_id": add_ev("code", f"ref.{access_type}", ln, _snippet(text, m.start()), f"Riverpod ref.{access_type}")})

    # AutoRoute annotations
    for m in AUTOROUTE_RE.finditer(text):
        ln = _line_no(text, m.start())
        parsed["annotations"].append({"kind": "AutoRoute", "line": ln, "file": rel,
                                      "evidence_id": add_ev("code", "@RoutePage", ln, _snippet(text, m.start(), 2), "AutoRoute page annotation")})

    # Localization .tr
    for m in TR_RE.finditer(text):
        ln = _line_no(text, m.start())
        raw = m.group(0)
        parsed["localizations"].append({"key": raw, "line": ln, "file": rel,
                                         "evidence_id": add_ev("code", "localization", ln, _snippet(text, m.start()), "Localization key with .tr")})

    # @freezed / @JsonSerializable models
    for m in FREEZED_RE.finditer(text):
        ln = _line_no(text, m.start())
        parsed["model_annotations"].append({"kind": "freezed", "line": ln, "file": rel,
                                             "evidence_id": add_ev("code", "@freezed", ln, _snippet(text, m.start(), 3), "Freezed model annotation")})
    for m in JSON_SERIAL_RE.finditer(text):
        ln = _line_no(text, m.start())
        parsed["model_annotations"].append({"kind": "JsonSerializable", "line": ln, "file": rel,
                                             "evidence_id": add_ev("code", "@JsonSerializable", ln, _snippet(text, m.start(), 3), "JsonSerializable model annotation")})

    # JSON fields (all patterns with json['key'])
    _seen = set()
    for m in re.finditer(r"json\s*\[\s*['\"]([^'\"]+)['\"]\s*\]\s*(?:as\s+([\w<>?.,\s()]+))?", text):
        raw_name = m.group(1)
        type_hint = m.group(2)
        if raw_name in _seen:
            continue
        _seen.add(raw_name)
        ln = _line_no(text, m.start())
        # Infer field name: look for word(s) before json[ on same line
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[line_start:m.start()]
        fm = re.search(r'(\w+)\s*[:=]\s*', before)
        field_name = fm.group(1) if fm else raw_name
        qname = f"{current_class}.{field_name}"
        parsed["fields"].append({
            "owner": current_class, "field": field_name, "raw_json_field": raw_name,
            "type_hint": type_hint, "line": ln, "qualified_name": qname,
            "evidence_id": add_ev("code", qname, ln, _snippet(text, m.start()), "Maps JSON/API field into Dart model"),
        })

    # Conditions (visible/enabled)
    for m in VISIBILITY_RE.finditer(text):
        expr = m.group(1).strip()
        if "=>" in expr or expr in {"null", "()"}:
            continue
        ln = _line_no(text, m.start())
        fields = sorted(set(IDENT_RE.findall(expr)) - {"true", "false", "null", "const", "final", "var"})
        if not fields and not any(t in expr for t in ("==", "!=", "&&", "||", "can", "status")):
            continue
        target = _nearby_widget_target(text, m.start())
        parsed["conditions"].append({"expression": expr, "target": target, "line": ln, "fields": fields,
                                     "evidence_id": add_ev("code", target, ln, _snippet(text, m.start(), 2), "UI condition/handler expression")})

    return parsed

def _nearest(method_spans: list[tuple[int, str]], offset: int) -> str | None:
    before = [name for start, name in method_spans if start <= offset]
    return before[-1] if before else None

def _nearby_widget_target(text: str, offset: int) -> str:
    window = text[offset:offset + 500]
    bm = TEXT_RE.search(window)
    if bm:
        return f"UI element text: {bm.group(1)}"
    return "UI condition"

def build_index(
    repo_path: str | Path,
    semble_candidate_files: list[str] | None = None,
    db_path: str | None = None,
) -> AppIndex:
    repo = Path(repo_path).resolve()
    cache_file = db_path or str(repo / ".app-context-cache.db")
    db = init_db(cache_file)
    db.execute("PRAGMA busy_timeout = 5000")

    index = AppIndex(repo_path=repo)
    dart_files = [p for p in repo.rglob("*.dart") if not _should_ignore(p)]

    if semble_candidate_files:
        wanted = set(semble_candidate_files)
        dart_files = [p for p in dart_files if str(p.relative_to(repo)) in wanted or p.name in wanted]

    index.dart_files = len(dart_files)

    cached, reparsed = 0, 0
    for file in dart_files:
        rel = str(file.relative_to(repo))
        stat = file.stat()
        mtime, size = stat.st_mtime, stat.st_size
        row = db.execute("SELECT parsed_json FROM file_cache WHERE rel_path=? AND mtime=? AND size=?",
                         (rel, mtime, size)).fetchone()
        if row:
            parsed = json.loads(row["parsed_json"])
            cached += 1
        else:
            parsed = _parse_single_file(file, repo)
            db.execute("INSERT OR REPLACE INTO file_cache(rel_path, mtime, size, parsed_json) VALUES (?,?,?,?)",
                       (rel, mtime, size, json.dumps(parsed, ensure_ascii=False)))
            reparsed += 1

        for s in parsed.get("screens", []):
            index.screens[s["name"]] = ScreenInfo(name=s["name"], file=rel, route=s.get("route"),
                                                   visible_texts=s.get("visible_texts", []), evidence_ids=[s["evidence_id"]])
        for a in parsed.get("api_calls", []):
            index.api_calls.append(ApiCall(method=a["method"], path_template=a["path"], client_method=a["client_method"],
                                            file=rel, line=a["line"], evidence_id=a["evidence_id"]))
        for f in parsed.get("fields", []):
            fi = FieldInfo(owner=f["owner"], field=f["field"], raw_json_field=f["raw_json_field"],
                           type_hint=f.get("type_hint"), file=rel, line=f["line"], evidence_id=f["evidence_id"])
            index.fields[fi.qualified_name] = fi
        for c in parsed.get("conditions", []):
            index.conditions.append(ConditionInfo(expression=c["expression"], target=c["target"], file=rel,
                                                   line=c["line"], fields=c.get("fields", []), evidence_id=c["evidence_id"]))
        for e in parsed.get("evidence", []):
            index.evidence[e["id"]] = Evidence(id=e["id"], source_type=e["source_type"], file=rel,
                                                  symbol=e["symbol"], lines=str(e["line"]), snippet=e["snippet"], why_relevant=e["why"])
        # New patterns
        for h in parsed.get("hooks", []):
            index.hooks.append(h)
        for ann in parsed.get("annotations", []):
            index.annotations.append(ann)
        for ma in parsed.get("model_annotations", []):
            index.model_annotations.append(ma)

    db.execute("INSERT OR REPLACE INTO repo_meta(repo_path, last_scan_ts, dart_files) VALUES (?,?,?)",
               (str(repo), datetime.now(timezone.utc).isoformat(), len(dart_files)))
    db.commit()
    db.close()

    print(f"[indexer] cached={cached}  reparsed={reparsed}  total={len(dart_files)}")
    return index

def _should_ignore(p: Path) -> bool:
    skip = {".git", ".dart_tool", "build", "ios", "android", "web", "macos", "windows", "linux"}
    return bool(set(p.parts) & skip) or p.name.endswith(".g.dart") or p.name.endswith(".freezed.dart")

def git_info(repo_path: str | Path) -> dict:
    repo = Path(repo_path)
    def run(args: list[str]) -> str | None:
        try:
            return subprocess.check_output(args, cwd=repo, stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            return None
    return {"git_commit": run(["git", "rev-parse", "HEAD"]),
            "git_branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
            "dirty": bool(run(["git", "status", "--porcelain"]))}
