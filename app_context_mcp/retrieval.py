from __future__ import annotations

from pathlib import Path
from typing import Any

from semble.index import SembleIndex


class SembleRetrieval:
    """Lightweight Semble hybrid (semantic + BM25) search for Flutter codebases.

    Usage:
        retriever = SembleRetrieval()
        retriever.index_repo('/path/to/flutter_app')
        results = retriever.search('cancel button visible logic', top_k=10)
        files = retriever.rank_files('cancel button visible logic', top_k=10)
    """

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or "minishlab/potion-code-16M"
        self.index: SembleIndex | None = None
        self.repo_path: Path | None = None

    def warmup(self) -> None:
        """Preload embedding model eagerly (idempotent)."""
        if self.index is not None:
            return
        from semble.index.dense import load_model
        load_model(self._model_name)

    def index_repo(self, repo_path: str | Path) -> None:
        """Index repo. Includes code + text files."""
        self.warmup()
        repo = Path(repo_path).expanduser().resolve()
        if not repo.exists():
            raise FileNotFoundError(f"Repo not found: {repo}")
        self.index = SembleIndex.from_path(
            str(repo),
            include_text_files=True,
        )
        self.repo_path = repo

    def search(self, query: str, top_k: int = 10, filter_languages: list[str] | None = None) -> list[Any]:
        """Hybrid semantic + BM25 search."""
        if self.index is None:
            raise RuntimeError("No index loaded. Call .index_repo() first.")
        results = self.index.search(query, top_k=top_k, rerank=True, filter_languages=filter_languages)
        return results

    def search_and_stats(self, query: str, top_k: int = 10, filter_languages: list[str] | None = None) -> dict[str, Any]:
        """Search + token savings stats."""
        if self.index is None:
            raise RuntimeError("No index loaded")
        # SembleIndex.search does not return stats directly; stats are saved to file.
        results = self.index.search(query, top_k=top_k, rerank=True, filter_languages=filter_languages)
        # We can compute rough stats from file count.
        files_in_results = {r.chunk.file_path for r in results if r.chunk.file_path}
        return {
            "results": results,
            "stats": {
                "files_matched": len(files_in_results),
                "chunks_matched": len(results),
                "total_indexed_chunks": len(self.index.chunks),
                "total_indexed_files": len(self.index._file_mapping) if hasattr(self.index, '_file_mapping') else None,
            },
        }

    def rank_files(self, query: str, top_k: int = 10) -> list[str]:
        """Unique file paths ranked for a query."""
        results = self.search(query, top_k=top_k)
        files: list[str] = []
        for r in results:
            fp = r.chunk.file_path if r.chunk.file_path else None
            if fp and fp not in files:
                files.append(fp)
        return files

    def search_related_terms(
        self,
        query: str,
        top_k: int = 10,
        filter_languages: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Results shaped for Flutter trace engine."""
        results = self.search(query, top_k=top_k, filter_languages=filter_languages)
        out: list[dict[str, Any]] = []
        for r in results:
            chunk = r.chunk
            snippet = chunk.content[:600] if getattr(chunk, "content", None) else None
            fp = chunk.file_path or None
            out.append({
                "file": fp,
                "lines": f"{chunk.start_line}-{chunk.end_line}" if chunk.start_line and chunk.end_line else None,
                "snippet": snippet,
                "score": float(r.score),
                "is_code": (Path(fp).suffix if fp else "") in {".dart", ".kt", ".swift", ".proto", ".graphql"},
                "language": chunk.language if chunk.language else None,
            })
        return out

    def wipe(self) -> None:
        self.index = None
        self.repo_path = None

    @property
    def ready(self) -> bool:
        return self.index is not None
