from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_APP_CONTEXT_DIR = ".app-context"
_CACHE_FILE = "index.cache.json"
_DECISION_FILE = "decisions.jsonl"
_CONVENTIONS_FILE = "conventions.json"
_KNOWN_ISSUES_FILE = "known-issues.json"
_API_CACHE_FILE = "api-cache.json"


def _project_dir(repo_path: str | Path) -> Path:
    return Path(repo_path).resolve() / _APP_CONTEXT_DIR


def ensure_dir(repo_path: str | Path) -> Path:
    d = _project_dir(repo_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Index Cache ──────────────────────────────────────────────────────────

def save_index_cache(repo_path: str | Path, cache_data: dict[str, Any]) -> None:
    d = ensure_dir(repo_path)
    cache_data["_meta"] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cache_version": "0.2.0",
    }
    with open(d / _CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"saved index cache to {d / _CACHE_FILE}")


def load_index_cache(repo_path: str | Path) -> dict[str, Any] | None:
    d = _project_dir(repo_path)
    path = d / _CACHE_FILE
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("_meta", {})
        logger.info(f"loaded index cache ({meta.get('cached_at', '?')})")
        return data
    except Exception as exc:
        logger.warning(f"failed to load cache: {exc}")
        return None


# ── Decision Log ───────────────────────────────────────────────────────

Record = dict[str, Any]


def append_decision(repo_path: str | Path, record: dict[str, Any]) -> None:
    d = ensure_dir(repo_path)
    record["ds_ts"] = datetime.now(timezone.utc).isoformat()
    with open(d / _DECISION_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def load_decisions(repo_path: str | Path, since_hours: int | None = None) -> list[Record]:
    d = _project_dir(repo_path)
    path = d / _DECISION_FILE
    if not path.exists():
        return []
    records: list[Record] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if since_hours and r.get("ds_ts"):
                    ts = datetime.fromisoformat(r["ds_ts"])
                    if (datetime.now(timezone.utc) - ts).total_seconds() > since_hours * 3600:
                        continue
                records.append(r)
            except Exception:
                continue
    return records


# ── Conventions ──────────────────────────────────────────────────────────

def save_conventions(repo_path: str | Path, conventions: dict[str, Any]) -> None:
    d = ensure_dir(repo_path)
    with open(d / _CONVENTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(conventions, f, ensure_ascii=False, indent=2)


def load_conventions(repo_path: str | Path) -> dict[str, Any]:
    d = _project_dir(repo_path)
    path = d / _CONVENTIONS_FILE
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ── Known Issues ─────────────────────────────────────────────────────────

def save_known_issues(repo_path: str | Path, issues: list[dict[str, Any]]) -> None:
    d = ensure_dir(repo_path)
    with open(d / _KNOWN_ISSUES_FILE, "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=2, default=str)


def load_known_issues(repo_path: str | Path) -> list[dict[str, Any]]:
    d = _project_dir(repo_path)
    path = d / _KNOWN_ISSUES_FILE
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ── API Shape Cache ─────────────────────────────────────────────────────

def save_api_cache(repo_path: str | Path, api_shapes: dict[str, Any]) -> None:
    d = ensure_dir(repo_path)
    with open(d / _API_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(api_shapes, f, ensure_ascii=False, indent=2)


def load_api_cache(repo_path: str | Path) -> dict[str, Any]:
    d = _project_dir(repo_path)
    path = d / _API_CACHE_FILE
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ── Hermes Memory Bridge (project-scoped) ──────────────────────────────

_HERMES_MEMORY_DIR = Path.home() / ".hermes" / "agent-memory"
_HERMES_LOG_FILE = _HERMES_MEMORY_DIR / "app-context-mcp-calls.jsonl"


def load_hermes_memory_for_repo(repo_path: str | Path, limit: int = 50) -> list[Record]:
    """Load Hermes agent memory entries relevant to this repo."""
    entries: list[Record] = []
    repo = str(Path(repo_path).resolve())

    # Look for app-context-mcp calls log first
    if _HERMES_LOG_FILE.exists():
        with open(_HERMES_LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if repo in r.get("repo", ""):
                    entries.append(r)
            except Exception:
                continue

    # Also look for session search index if available
    session_file = _HERMES_MEMORY_DIR / "sessions" / "index.json"
    if session_file.exists():
        try:
            with open(session_file, encoding="utf-8") as f:
                sessions = json.load(f)
            for sid, sinfo in sessions.items():
                if repo in str(sinfo.get("repo_path", "")):
                    entries.append({
                        "source": "hermes_session",
                        "session_id": sid,
                        "title": sinfo.get("title", ""),
                        "last_active": sinfo.get("last_active", ""),
                    })
        except Exception:
            pass

    return entries


# ── Utility: clear / rebuild ─────────────────────────────────────────────

def clear_cache(repo_path: str | Path) -> None:
    d = _project_dir(repo_path)
    for f in (_CACHE_FILE, _DECISION_FILE, _CONVENTIONS_FILE, _KNOWN_ISSUES_FILE, _API_CACHE_FILE):
        p = d / f
        if p.exists():
            p.unlink()
            logger.info(f"removed {p}")


def cache_age_seconds(repo_path: str | Path) -> float:
    """Return age of index cache in seconds. -1 if no cache."""
    d = _project_dir(repo_path)
    path = d / _CACHE_FILE
    if not path.exists():
        return -1
    mtime = path.stat().st_mtime
    return datetime.now().timestamp() - mtime


def is_cache_fresh(repo_path: str | Path, max_age_seconds: int = 86400) -> bool:
    age = cache_age_seconds(repo_path)
    return 0 <= age <= max_age_seconds