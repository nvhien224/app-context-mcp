from __future__ import annotations

import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import Any


class RAGIndex:
    """Lightweight local-only RAG for business docs, contracts, wiki, API specs.

    Stores documents in SQLite; supports FTS5 + cosine-similarity vector search
    using in-memory numpy (no external vector DB).
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = Path(tempfile.gettempdir()) / "app_context_rag.db"
        else:
            db_path = Path(db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                source_type TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                extra_tags TEXT NOT NULL DEFAULT ''
            )
        """)
        try:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    content='documents',
                    source, source_type, title, content, extra_tags
                )
            """)
        except sqlite3.OperationalError:
            pass
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_embeddings (
                doc_id INTEGER PRIMARY KEY,
                vector BLOB,
                FOREIGN KEY(doc_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        self.conn.commit()

    def add(
        self,
        source: str,
        source_type: str,
        title: str,
        content: str,
        extra_tags: str = "",
        vector: list[float] | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO documents(source, source_type, title, content, extra_tags) VALUES (?,?,?,?,?)",
            (source, source_type, title, content, extra_tags),
        )
        doc_id = cur.lastrowid
        if vector is not None:
            self.conn.execute(
                "INSERT OR REPLACE INTO doc_embeddings(doc_id, vector) VALUES (?,?)",
                (doc_id, self._pack_vector(vector)),
            )
        self.conn.commit()
        return doc_id if doc_id is not None else -1

    def _pack_vector(self, vec: list[float]) -> bytes:
        """Pack float32 vector into bytes."""
        return struct.pack(f"{len(vec)}f", *vec)

    def _unpack_vector(self, data: bytes) -> list[float]:
        n = len(data) // 4
        return list(struct.unpack(f"{n}f", data))

    def search(
        self,
        query: str,
        top_k: int = 10,
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid FTS5 + optional semantic re-ranking.

        If no embeddings, FTS5 ranking stands on its own.
        If embeddings exist, compute cosine similarity and do RRF with FTS ranking.
        """
        fts_results = self._fts_search(query, top_k=top_k * 3, source_type=source_type)
        if not fts_results:
            return []

        semantic_results = self._vector_search(query, top_k=top_k * 3) if self._has_embeddings() else []
        merged = self._rrf_merge(fts_results, semantic_results, k=top_k)
        return merged[:top_k]

    def _fts_search(self, query: str, top_k: int, source_type: str | None = None) -> list[dict[str, Any]]:
        """FTS5 BM25 search."""
        try:
            if source_type:
                rows = self.conn.execute(
                    "SELECT d.id, source, source_type, title, content, extra_tags, rank "
                    "FROM documents d JOIN documents_fts ON d.id = documents_fts.rowid "
                    "WHERE documents_fts MATCH ? AND source_type = ? "
                    "ORDER BY rank LIMIT ?",
                    (query, source_type, top_k),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT d.id, source, source_type, title, content, extra_tags, rank "
                    "FROM documents d JOIN documents_fts ON d.id = documents_fts.rowid "
                    "WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, top_k),
                ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 may not have rows, or triggered by update logic need resync.
            return []
        results: list[dict[str, Any]] = []
        for row in rows:
            doc_id, source, stype, title, content, tags, rank = row
            results.append({
                "doc_id": doc_id,
                "source": source,
                "source_type": stype,
                "title": title,
                "content": content[:800],
                "tags": tags,
                "score": float(rank) if rank is not None else 0.0,
                "search_type": "fts5",
            })
        return results

    def _has_embeddings(self) -> bool:
        count = self.conn.execute("SELECT COUNT(*) FROM doc_embeddings").fetchone()[0]
        return count > 0

    def _vector_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """Simple in-memory cosine similarity against query TF-IDF vector.
        """
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
        except ImportError:
            return []

        docs = self.conn.execute(
            "SELECT d.id, source, source_type, title, content, extra_tags "
            "FROM documents d JOIN doc_embeddings e ON d.id = e.doc_id"
        ).fetchall()
        if not docs:
            return []

        texts = [row[4] for row in docs]  # content
        vecs = []
        for row in docs:
            emb_row = self.conn.execute("SELECT vector FROM doc_embeddings WHERE doc_id=?", (row[0],)).fetchone()
            if emb_row and emb_row[0]:
                vecs.append(self._unpack_vector(emb_row[0]))
            else:
                return []

        # Build TF-IDF over contents and compute similarity to query
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), max_features=5000)
        X = vectorizer.fit_transform(texts)
        q_vec = vectorizer.transform([query])
        scores = cosine_similarity(q_vec, X).flatten()

        results: list[dict[str, Any]] = []
        indexed = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_k]
        for idx in indexed:
            row = docs[idx]
            results.append({
                "doc_id": row[0],
                "source": row[1],
                "source_type": row[2],
                "title": row[3],
                "content": row[4][:800],
                "tags": row[5],
                "score": float(scores[idx]),
                "search_type": "semantic",
            })
        return results

    def _rrf_merge(
        self,
        fts_results: list[dict[str, Any]],
        sem_results: list[dict[str, Any]],
        k: int = 60,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """RRF cross-merge FTS5 and semantic results."""
        scores: dict[int, float] = {}
        info: dict[int, dict] = {}

        for rank, r in enumerate(fts_results):
            rid = r["doc_id"]
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            info[rid] = r

        for rank, r in enumerate(sem_results):
            rid = r["doc_id"]
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            if rid not in info:
                info[rid] = r

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        out: list[dict[str, Any]] = []
        for rid, rrf_score in ranked[:top_k]:
            merged = dict(info[rid])
            merged["rrf_score"] = round(rrf_score, 6)
            out.append(merged)
        return out

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def wipe(self) -> None:
        """Drop all data."""
        try:
            self.conn.execute("DROP TABLE IF EXISTS documents")
            self.conn.execute("DROP TABLE IF EXISTS documents_fts")
            self.conn.execute("DROP TABLE IF EXISTS doc_embeddings")
        except Exception:
            pass
        self._init_tables()
