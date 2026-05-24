from __future__ import annotations

from pathlib import Path

from .retrieval import SembleRetrieval


def semble_ranked_files(repo_path: str | Path, query: str, top_k: int = 15) -> list[str]:
    """Return Semble-ranked Dart file paths for a query.

    This is a thin wrapper for the trace engine to pre-filter candidates.
    If Semble fails or repo is too small, return an empty list for fallback.
    """
    try:
        sr = SembleRetrieval()
        sr.index_repo(repo_path)
        return sr.rank_files(query, top_k=top_k)
    except Exception:
        return []
