from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class IndexCache:
    """Content-addressable cache for Semble index + CodeGraph.

    Arkon pattern: compute SHA256(content) → skip re-index khi code không đổi.
    Cache persist tại .app-context-cache/ trong repo.
    """

    def __init__(self, repo_path: str | Path):
        self.repo = Path(repo_path).resolve()
        self.cache_dir = self.repo / ".app-context-cache"
        self.cache_dir.mkdir(exist_ok=True)
        self._index_hash_file = self.cache_dir / "index_hash.txt"
        self._index_file = self.cache_dir / "semble_index.pkl"
        self._codegraph_file = self.cache_dir / "code_graph.json"

    def _compute_repo_hash(self) -> str:
        """Walk all .dart, .json, .yaml files → mtime+size hash.

        Quicker nhanh hơn content hash, đủ detect bất kỳ thay đổi.
        """
        h = hashlib.sha256()
        for suffix in (".dart", ".yaml", ".yml", ".json", ".md"):
            for f in sorted(self.repo.rglob(f"*{suffix}")):
                try:
                    stat = f.stat()
                    h.update(f"{f}:{stat.st_mtime}:{stat.st_size}\n".encode())
                except OSError:
                    pass
        return h.hexdigest()

    def is_fresh(self) -> bool:
        """Return True nếu cache còn valid (repo không đổi)."""
        if not self._index_hash_file.exists():
            return False
        current = self._compute_repo_hash()
        stored = self._index_hash_file.read_text().strip()
        return current == stored

    def save_index_meta(self, index: Any, codegraph: dict[str, Any] | None) -> None:
        """Persist cache metadata sau khi index thành công."""
        self._index_hash_file.write_text(self._compute_repo_hash(), encoding="utf-8")
        # Semble index có save riêng. CodeGraph JSON persist nhẹ.
        if codegraph:
            self._codegraph_file.write_text(
                json.dumps(codegraph, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def load_codegraph(self) -> dict[str, Any] | None:
        if self._codegraph_file.exists():
            return json.loads(self._codegraph_file.read_text(encoding="utf-8"))
        return None

    def clear(self) -> None:
        """Force invalidate."""
        for f in [self._index_hash_file, self._index_file, self._codegraph_file]:
            f.unlink(missing_ok=True)
