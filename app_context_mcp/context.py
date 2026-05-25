from __future__ import annotations
from app_context_mcp.retrieval import SembleRetrieval

from .cache import repo_cache, _get_search_cache
from .code_graph import build_code_graph
from .graph_retrieval import build_graph_evidence_pack
from .indexer import build_index, git_info
from .models import AppIndex, Evidence
from .rag_index import RAGIndex
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .graph_builder import build_graph

def ask_app_context(
    repo_path: str | Path,
    question: str,
    screenshot_text: str | None = None,
    user_role: str | None = None,
    desired_behavior: str | None = None,
    max_results: int = 20,
    use_semble: bool = True,
    token_budget: int | None = None,
) -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    cache_key = f"idx:{repo}:{repo.stat().st_mtime if repo.exists() else 0}"

    # ── L1 cache: in-memory hot index ──
    index = repo_cache().get(cache_key)
    if index is None:
        semble_candidates = None
        q = f"{question} {screenshot_text or ''} {desired_behavior or ''}".strip()
        if use_semble:
            try:
                sr = SembleRetrieval()
                sr.index_repo(repo)
                semble_candidates = sr.rank_files(q, top_k=max_results)
            except Exception:
                semble_candidates = None
        index = build_index(repo, semble_candidate_files=semble_candidates)
        # Build import graph + symbol graph + execution flows
        build_graph(index)
        repo_cache().set(cache_key, index, ttl=60.0)

    role = user_role or "NON_TECH"
    query_text = " ".join(part for part in [question, screenshot_text, desired_behavior] if part).lower()

    # ── L2 cache: search query cache keyed by git state ──
    info = git_info(repo)
    git_sha = info.get("git_commit") or ""
    git_branch = info.get("git_branch") or ""
    search_key = f"srch:{repo}:{hashlib.md5((query_text[:160] + git_sha + git_branch).encode()).hexdigest()}"
    sc = _get_search_cache(db_path=str(repo / ".app-context-cache.db"))
    cached_result = sc.get(search_key)
    if cached_result is not None:
        result = dict(cached_result)
        result["_cached"] = True
        return result

    intents = detect_intents(query_text)

    # RAG semantic search — augment keyword search with vector similarity
    rag_hits: list[dict] = []
    try:
        rag = RAGIndex(db_path=repo / ".app-context-rag.db")
        rag_hits = rag.search(query_text, top_k=max_results)
    except Exception:
        pass

    screen_candidates = rank_screens(index, query_text, max_results=max_results)
    condition_hits = rank_conditions(index, query_text, max_results=max_results)
    api_hits = rank_apis(index, query_text, max_results=max_results)
    field_hits = rank_fields(index, query_text, max_results=max_results)
    semble_evidence = {"used": use_semble, "candidate_files": []}

    evidence_ids: set[str] = set()
    for item in screen_candidates:
        evidence_ids.update(item.get("evidence_refs", []))
    for item in condition_hits:
        evidence_ids.update(item.get("evidence_refs", []))
    for item in api_hits:
        evidence_ids.update(item.get("evidence_refs", []))
    for item in field_hits:
        evidence_ids.update(item.get("evidence_refs", []))

    status = decide_status(screen_candidates, condition_hits, api_hits, field_hits)
    unknowns = build_unknowns(status, screenshot_text, api_hits)
    evidence_registry = [evidence_to_dict(index.evidence[eid]) for eid in sorted(evidence_ids) if eid in index.evidence]

    # Token budget clamping
    if token_budget is not None:
        evidence_registry = _clamp_evidence_token(evidence_registry, token_budget)

    result: dict[str, Any] = {
        "status": status,
        "query_interpretation": {
            "original_question": question,
            "detected_intents": intents,
            "keywords": extract_keywords(query_text),
            "role_assumption": role,
            "screenshot_text_present": bool(screenshot_text),
        },
        "screen_candidates": screen_candidates,
        "case_flow_candidates": build_case_flow_candidates(condition_hits, api_hits),
        "code_trace_graph": build_trace_graph(screen_candidates, condition_hits, api_hits, field_hits),
        "ui_elements": build_ui_elements(condition_hits),
        "api_usage": api_hits,
        "api_field_lineage": field_hits,
        "current_logic_facts": build_logic_facts(screen_candidates, condition_hits, api_hits, field_hits),
        "desired_behavior_analysis": build_desired_behavior_analysis(desired_behavior or question, field_hits, api_hits) if wants_workaround(query_text) else {},
        "five_w_one_h": build_5w1h(screen_candidates, condition_hits, api_hits, field_hits),
        "socratic_followups": build_socratic_followups(query_text),
        "information_needed_from_user": build_information_needed(status, screen_candidates),
        "evidence_registry": evidence_registry,
        "unknowns_and_limits": unknowns,
        "index_info": build_index_info(index),
        "search_info": {**semble_evidence, "query": query_text[:200], "rag_hits": rag_hits[:5]},
        "code_graph": build_code_graph(index).export(),
        "graph_evidence_pack": build_graph_evidence_pack(index, screen_candidates, condition_hits, api_hits, field_hits),
        "import_graph": build_import_graph(index),
        "execution_flows": build_execution_flows(index, screen_candidates),
        "impact_analysis": build_impact_analysis(index, screen_candidates, condition_hits, api_hits),
    }

    # ── Fallback: raw grep if everything returns empty (issue #10) ──
    if status == "not_found":
        fallback = _fallback_raw_grep(repo, query_text, max_results=max_results)
        if fallback:
            status = "partial"
            result["fallback_candidates"] = fallback
        else:
            result["fallback_candidates"] = []
    else:
        result["fallback_candidates"] = []

    result["_cached"] = False

    # Cache search result for 30s — invalidated on git commit/branch change
    sc = _get_search_cache(db_path=str(repo / ".app-context-cache.db"))
    sc.set(search_key, dict(result), ttl=30.0)
    return result


def _clamp_evidence_token(registry: list[dict], budget: int) -> list[dict]:
    """Clamp evidence registry to not exceed token budget."""
    max_chars = budget * 3
    total = 0
    out = []
    for ev in registry:
        ev_copy = dict(ev)
        ev_copy["snippet"] = ev_copy.get("snippet", "")[:800]
        size = len(json.dumps(ev_copy))
        if total + size > max_chars:
            break
        total += size
        out.append(ev_copy)
    return out
def detect_intents(text: str) -> list[str]:
    intents = []
    if any(k in text for k in ["màn", "screen", "route", "page"]):
        intents.append("screen_identification")
    if any(k in text for k in ["logic", "vì sao", "why", "condition", "điều kiện", "ẩn", "hiện", "enable", "disable"]):
        intents.append("business_logic")
    if any(k in text for k in ["api", "endpoint", "field", "trường"]):
        intents.append("api_usage")
    if any(k in text for k in ["workaround", "muốn", "cần", "thêm field", "backend", "be"]):
        intents.append("api_workaround")
    return intents or ["general_app_context"]


def extract_keywords(text: str) -> list[str]:
    stop = {"màn", "này", "vì", "sao", "không", "api", "field", "logic", "the", "and", "what", "how"}
    tokens = [tok.strip(" ?.,:;!()[]{}\"'").lower() for tok in text.replace("/", " ").split()]
    return sorted({tok for tok in tokens if len(tok) >= 3 and tok not in stop})[:30]


def _fallback_raw_grep(repo: Path, query: str, max_results: int = 20) -> list[dict[str, Any]]:
    """Issue #10: fallback grep across all Dart files when semantic search returns zero."""
    import subprocess
    keywords = extract_keywords(query)[:5]
    results: list[dict[str, Any]] = []
    seen = set()
    for kw in keywords:
        try:
            proc = subprocess.run(
                ["rg", "-i", "-l", "-t", "dart", "--max-count", "50", kw, str(repo)],
                capture_output=True, text=True, timeout=10
            )
            for line in proc.stdout.strip().splitlines()[:max_results // len(keywords) + 1]:
                fpath = Path(line)
                if not fpath.exists():
                    continue
                rel = str(fpath.relative_to(repo))
                if rel in seen:
                    continue
                seen.add(rel)
                # Get snippet
                snippet_proc = subprocess.run(
                    ["rg", "-i", "--context", "2", kw, str(fpath)],
                    capture_output=True, text=True, timeout=5
                )
                results.append({
                    "file": rel,
                    "match_keyword": kw,
                    "snippet": snippet_proc.stdout[:500],
                })
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # ripgrep not installed or timed out; try python fallback
            for p in repo.rglob("*.dart"):
                text = p.read_text(encoding="utf-8", errors="ignore")
                if kw in text.lower():
                    rel = str(p.relative_to(repo))
                    if rel in seen:
                        continue
                    seen.add(rel)
                    lines = text.splitlines()
                    for i, line in enumerate(lines):
                        if kw in line.lower():
                            snippet = "\n".join(lines[max(0, i-2):min(len(lines), i+3)])
                            results.append({
                                "file": rel,
                                "match_keyword": kw,
                                "snippet": snippet[:500],
                            })
                            break
                    if len(results) >= max_results:
                        break
        if len(results) >= max_results:
            break
    return results[:max_results]


def rank_screens(index: AppIndex, query: str, max_results: int) -> list[dict[str, Any]]:
    results = []
    for screen in index.screens.values():
        haystack = " ".join([screen.name, screen.route or "", *screen.visible_texts]).lower()
        score = score_match(query, haystack)
        if score > 0:
            results.append({
                "screen_name": screen.name,
                "route": screen.route,
                "file": screen.file,
                "confidence": min(0.95, 0.35 + score / 10),
                "matched_by": matched_terms(query, haystack),
                "visible_texts": screen.visible_texts,
                "evidence_refs": screen.evidence_ids,
            })
    return sorted(results, key=lambda x: x["confidence"], reverse=True)[:max_results]


def rank_conditions(index: AppIndex, query: str, max_results: int) -> list[dict[str, Any]]:
    results = []
    for cond in index.conditions:
        haystack = " ".join([cond.expression, cond.target, *cond.fields]).lower()
        score = score_match(query, haystack)
        # UI conditions are useful context for any logic question, even if exact token match is weak.
        if score > 0 or any(k in query for k in ["ẩn", "hiện", "button", "nút", "condition", "điều kiện", "hủy", "cancel"]):
            results.append({
                "target": cond.target,
                "kind": "condition",
                "raw_expression": cond.expression,
                "plain_meaning": plain_condition(cond.expression),
                "depends_on_fields": cond.fields,
                "file": cond.file,
                "line": cond.line,
                "confidence": min(0.9, 0.45 + score / 10),
                "evidence_refs": [cond.evidence_id],
            })
    return sorted(results, key=lambda x: x["confidence"], reverse=True)[:max_results]


def rank_apis(index: AppIndex, query: str, max_results: int) -> list[dict[str, Any]]:
    results = []
    for api in index.api_calls:
        haystack = " ".join([api.method, api.path_template, api.client_method]).lower()
        score = score_match(query, haystack)
        if score > 0 or any(k in query for k in ["api", "field", "hủy", "cancel", "order", "đơn"]):
            results.append({
                "api_id": f"api:{api.method} {api.path_template}",
                "method": api.method,
                "path": api.path_template,
                "client_method": api.client_method,
                "repository_method": None,
                "called_by": [],
                "purpose_in_flow": infer_api_purpose(api.path_template, api.method),
                "request_fields": ["id"] if "{id}" in api.path_template else [],
                "response_fields_used": [],
                "contract_status": "unknown",
                "file": api.file,
                "line": api.line,
                "confidence": min(0.9, 0.4 + score / 10),
                "evidence_refs": [api.evidence_id],
            })
    return sorted(results, key=lambda x: x["confidence"], reverse=True)[:max_results]


def rank_fields(index: AppIndex, query: str, max_results: int) -> list[dict[str, Any]]:
    results = []
    for field in index.fields.values():
        haystack = " ".join([field.qualified_name, field.raw_json_field, field.type_hint or ""]).lower()
        score = score_match(query, haystack)
        if score > 0 or any(k in query for k in ["field", "trường", "api", "hủy", "cancel", "status"]):
            results.append({
                "api": infer_api_for_field(field),
                "raw_json_field": field.raw_json_field,
                "model_field": field.qualified_name,
                "type": field.type_hint,
                "nullable": bool(field.type_hint and field.type_hint.endswith("?")),
                "used_in": [],
                "transformations": [f"json['{field.raw_json_field}'] -> {field.qualified_name}"],
                "confidence": min(0.9, 0.45 + score / 10),
                "evidence_refs": [field.evidence_id],
            })
    return sorted(results, key=lambda x: x["confidence"], reverse=True)[:max_results]


def score_match(query: str, haystack: str) -> int:
    q_terms = set(extract_keywords(query))
    if not q_terms:
        return 0
    score = 0
    for term in q_terms:
        if term in haystack:
            score += 2 if len(term) > 4 else 1
    return score


def matched_terms(query: str, haystack: str) -> list[str]:
    return [term for term in extract_keywords(query) if term in haystack]


def decide_status(screens, conditions, apis, fields) -> str:
    if screens and (conditions or apis or fields):
        return "found"
    if screens or conditions or apis or fields:
        return "partial"
    return "not_found"


def build_unknowns(status: str, screenshot_text: str | None, apis: list[dict]) -> list[dict[str, str]]:
    unknowns = []
    if status != "found":
        unknowns.append({"type": "insufficient_static_evidence", "message": "Could not fully map the question to screen/API/condition evidence.", "impact": "Client should ask user for screen/route/API hint or inspect more context."})
    if screenshot_text:
        unknowns.append({"type": "screenshot_text_only", "message": "Screenshot was represented as text; no visual layout/OCR confidence is available inside MCP.", "impact": "Screen mapping may be ambiguous if visible text is shared."})
    if apis:
        unknowns.append({"type": "missing_runtime_trace", "message": "API usage is inferred from static code; no runtime request log was used.", "impact": "Endpoint may not execute in this exact state if guarded by runtime flags/permissions."})
    return unknowns


def build_case_flow_candidates(conditions, apis):
    candidates = []
    for cond in conditions[:5]:
        candidates.append({
            "case_name": infer_case_name(cond["target"], cond["raw_expression"]),
            "entry_points": [cond["target"]],
            "user_action": "Interact with related UI element",
            "business_goal": "Understand current UI/business behavior",
            "confidence": cond["confidence"],
            "evidence_refs": cond["evidence_refs"],
        })
    return candidates


def build_trace_graph(screens, conditions, apis, fields):
    nodes = []
    edges = []
    for s in screens[:3]:
        nodes.append({"id": f"screen:{s['screen_name']}", "type": "screen", "name": s["screen_name"], "file": s["file"]})
    for cond in conditions[:5]:
        cid = f"condition:{cond['target']}:{cond['raw_expression']}"
        nodes.append({"id": cid, "type": "ui_condition", "name": cond["target"], "expression": cond["raw_expression"]})
        for s in screens[:1]:
            edges.append({"from": f"screen:{s['screen_name']}", "to": cid, "relation": "has_condition"})
        for field in cond.get("depends_on_fields", []):
            fid = f"field:{field}"
            nodes.append({"id": fid, "type": "field_reference", "name": field})
            edges.append({"from": cid, "to": fid, "relation": "depends_on"})
    for api in apis[:5]:
        aid = api["api_id"]
        nodes.append({"id": aid, "type": "api_endpoint", "method": api["method"], "path": api["path"]})
    for field in fields[:8]:
        fid = f"model_field:{field['model_field']}"
        nodes.append({"id": fid, "type": "api_field", "name": field["raw_json_field"], "model_field": field["model_field"]})
        if apis:
            edges.append({"from": apis[0]["api_id"], "to": fid, "relation": "returns_or_maps_field"})
    return {"nodes": dedupe_nodes(nodes), "edges": edges}


def build_ui_elements(conditions):
    return [{
        "element_name": cond["target"],
        "element_type": "unknown_ui_element",
        "visible_text": cond["target"].replace("UI element text: ", "") if cond["target"].startswith("UI element text:") else None,
        "conditions": [cond],
        "event_handlers": [],
    } for cond in conditions]


def build_logic_facts(screens, conditions, apis, fields):
    facts = []
    for cond in conditions[:5]:
        facts.append({"fact": f"{cond['target']} is controlled by condition `{cond['raw_expression']}`.", "fact_type": "ui_condition", "confidence": "high" if cond["evidence_refs"] else "medium", "evidence_refs": cond["evidence_refs"]})
    for field in fields[:8]:
        facts.append({"fact": f"API/model field `{field['raw_json_field']}` maps to `{field['model_field']}`.", "fact_type": "api_field_usage", "confidence": "medium", "evidence_refs": field["evidence_refs"]})
    for api in apis[:5]:
        facts.append({"fact": f"Code calls `{api['method']} {api['path']}` via `{api['client_method']}`.", "fact_type": "api_usage", "confidence": "medium", "evidence_refs": api["evidence_refs"]})
    return facts


def wants_workaround(text: str) -> bool:
    return any(k in text for k in ["workaround", "muốn", "cần", "thêm field", "backend", "be", "api cần"])


def build_desired_behavior_analysis(desired: str, fields, apis):
    has_reason = any("reason" in f["raw_json_field"].lower() or "blocked" in f["raw_json_field"].lower() for f in fields)
    has_can = any(f["raw_json_field"].lower() in {"cancancel", "is_cancelable", "cancelable"} for f in fields)
    if has_reason:
        status = "supported"
        reason = "A reason-like field appears to exist in indexed model fields."
    elif has_can:
        status = "partially_supported"
        reason = "Current fields can indicate whether action is allowed, but not why it is blocked."
    else:
        status = "unknown"
        reason = "No explicit cancelability/reason field was confidently found."
    return {
        "detected_desired_behavior": desired,
        "current_support": {"status": status, "reason": reason},
        "current_gaps": [] if has_reason else [{"gap": "Cannot distinguish/display why cancellation is blocked from current indexed fields.", "missing_data": ["cancelBlockedReason"], "evidence_refs": []}],
        "api_options": [
            {"option_type": "add_response_field", "proposal": "Add `cancelBlockedReason` enum next to existing action/cancelability data.", "pros": ["Minimal response change", "Client can show deterministic reason"], "cons": ["Specific to this action unless generalized"], "risk": "medium"},
            {"option_type": "backend_driven_actions", "proposal": "Return `availableActions.cancel.enabled` plus `reason` from the relevant detail/actions API.", "pros": ["Backend remains source of truth", "Scales to many actions"], "cons": ["Larger API contract change"], "risk": "low"},
            {"option_type": "frontend_only_inference", "proposal": "Infer reason from existing status/delivery/payment fields.", "pros": ["No backend change"], "cons": ["Can drift from backend rules", "May be wrong for hidden rules"], "risk": "high"},
        ],
    }


def build_5w1h(screens, conditions, apis, fields):
    return {
        "who": ["User interacting with the matched screen"] if screens else [],
        "what": [fact for cond in conditions[:3] for fact in [f"UI behavior controlled by `{cond['raw_expression']}`"]],
        "when": ["When the screen renders after data/state is loaded"] if conditions else [],
        "where": [*(s["screen_name"] for s in screens[:3]), *(f"{a['method']} {a['path']}" for a in apis[:3])],
        "why": ["Because UI conditions depend on model/API fields shown in evidence"] if fields and conditions else [],
        "how": ["Match screen/context", "Trace UI condition", "Map condition fields to model/API fields", "Return file/line evidence"],
    }


def build_socratic_followups(text: str):
    qs = []
    if wants_workaround(text):
        qs.append({"question": "Rule này nên do backend làm source of truth hay frontend được phép suy ra từ status?", "why_it_matters": "Nếu backend quyết định, API nên trả action policy/reason thay vì FE hardcode.", "related_gap": "source_of_truth"})
        qs.append({"question": "Người dùng cần thấy lý do ngay trên màn hình hay chỉ sau khi bấm action?", "why_it_matters": "Nếu cần thấy ngay, detail API cần trả reason; nếu chỉ sau khi bấm, validate/action endpoint có thể đủ.", "related_gap": "api_shape"})
    else:
        qs.append({"question": "Bạn có thể cung cấp screen/route hoặc text trên screenshot không?", "why_it_matters": "Giúp MCP giảm ambiguity khi nhiều màn dùng text giống nhau.", "related_gap": "screen_identification"})
    return qs


def build_information_needed(status, screens):
    if status == "found" and len(screens) == 1:
        return []
    return [{"field": "screen_context.screen_hint", "question": "Screen/route cụ thể là gì?", "why": "Giúp chọn đúng candidate khi screenshot/text mơ hồ."}]


def build_index_info(index: AppIndex):
    info = git_info(index.repo_path)
    return {
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "repo_path": str(index.repo_path),
        **info,
        "index_version": "0.1.0",
        "sources_indexed": {"dart_files": index.dart_files, "screens": len(index.screens), "api_calls": len(index.api_calls), "fields": len(index.fields), "conditions": len(index.conditions)},
    }


def evidence_to_dict(ev: Evidence):
    return {"id": ev.id, "source_type": ev.source_type, "file": ev.file, "symbol": ev.symbol, "lines": ev.lines, "snippet": ev.snippet, "why_relevant": ev.why_relevant}


def dedupe_nodes(nodes):
    seen = {}
    for node in nodes:
        seen[node["id"]] = node
    return list(seen.values())


def infer_api_purpose(path: str, method: str) -> str:
    if method == "GET":
        return "Load data for screen/state rendering"
    return "Trigger mutation/action from user flow"


def infer_api_for_field(field):
    return "unknown_static_api"


def infer_case_name(target: str, expr: str) -> str:
    joined = f"{target} {expr}".lower()
    if "cancel" in joined or "hủy" in joined or "cancancel" in joined:
        return "Cancel action"
    return target


# ── GitNexus-inspired helpers ───────────────────────────────────────────

def build_import_graph(index: AppIndex) -> dict[str, Any]:
    """Export import dependency graph for evidence pack."""
    reverse = index.import_graph_reverse
    # Top files by number of dependents
    dependents = {f: len(reverse.get(f, set())) for f in index.import_graph}
    top_imported = sorted(dependents.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return {
        "total_import_edges": len(index.imports),
        "total_files_with_imports": len(index.import_graph),
        "most_imported_files": [{"file": f, "imported_by": n} for f, n in top_imported],
        "sample_edges": [
            {"from": e.source_file, "to": e.target_file, "path": e.import_path, "line": e.line}
            for e in index.imports[:30]
        ],
    }


def build_execution_flows(index: AppIndex, screen_candidates: list[dict]) -> list[dict]:
    """Export execution flows related to matched screens."""
    matched_screen_names = {s["screen_name"] for s in screen_candidates}
    result: list[dict] = []
    for flow in index.execution_flows:
        # Only include flows that involve matched screens
        if flow.screens_involved and flow.screens_involved[0] in matched_screen_names:
            result.append({
                "name": flow.name,
                "steps": flow.steps,
                "screens_involved": flow.screens_involved,
                "apis_involved": flow.apis_involved,
                "fields_involved": flow.fields_involved,
            })
    # If no flows matched, return all flows (limited)
    if not result:
        for flow in index.execution_flows[:10]:
            result.append({
                "name": flow.name,
                "steps": flow.steps,
                "screens_involved": flow.screens_involved,
                "apis_involved": flow.apis_involved,
                "fields_involved": flow.fields_involved,
            })
    return result


def build_impact_analysis(
    index: AppIndex,
    screen_candidates: list[dict],
    condition_hits: list[dict],
    api_hits: list[dict],
) -> dict[str, Any]:
    """Estimate blast radius if user wants to change something."""
    impacted_screens = len(screen_candidates)
    impacted_conditions = len(condition_hits)
    impacted_apis = len(api_hits)

    # Collect files touched
    files_touched: set[str] = set()
    for s in screen_candidates:
        files_touched.add(s.get("file", ""))
    for c in condition_hits:
        files_touched.add(c.get("file", ""))
    for a in api_hits:
        files_touched.add(a.get("file", ""))
    files_touched.discard("")

    # Count files that import these files (reverse deps)
    reverse = index.import_graph_reverse
    downstream = set()
    for f in files_touched:
        downstream.update(reverse.get(f, set()))
    downstream -= files_touched  # only extras

    # Risk heuristic
    risk = "LOW"
    if len(files_touched) > 5 or len(downstream) > 3:
        risk = "MEDIUM"
    if len(files_touched) > 10 or len(downstream) > 8:
        risk = "HIGH"
    if impacted_apis > 0 and impacted_conditions > 0:
        risk = "MEDIUM"  # at least API + UI involved

    return {
        "risk_level": risk,
        "files_directly_touched": sorted(files_touched),
        "files_downstream_affected": sorted(downstream)[:20],
        "screens_affected": impacted_screens,
        "conditions_affected": impacted_conditions,
        "apis_affected": impacted_apis,
        "advice": (
            "Đổi ảnh hưởng nhiều files. Nên chạy tests + đọc kỹ downstream files trước khi edit."
            if risk in ("MEDIUM", "HIGH")
            else "Thay đổi phạm vi hẹp — an toàn để edit."
        ),
    }


def plain_condition(expr: str) -> str:
    return f"UI behavior depends on `{expr}`."
