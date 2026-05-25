"""Graph-traversal retrieval using CodeGraph.

Provides seed-based BFS expand, shortest path trace, feature clustering,
and downstream impact analysis via graph distance.
"""
from __future__ import annotations

from typing import Any

from .code_graph import CodeGraph, build_code_graph
from .models import AppIndex


def graph_expand_from_seeds(
    graph: CodeGraph,
    seed_screens: list[dict],
    seed_conditions: list[dict],
    seed_apis: list[dict],
    seed_fields: list[dict],
    depth: int = 2,
) -> dict[str, Any]:
    """Expand from keyword-matched nodes using BFS on code graph."""
    seeds: set[str] = set()

    for s in seed_screens:
        seeds.add(f"screen:{s['screen_name']}")

    for c in seed_conditions:
        seeds.add(f"condition:{c['file']}:{c['line']}")

    for a in seed_apis:
        aid = f"api:{a['method']} {a['path']}"
        if aid in graph.nodes:
            seeds.add(aid)

    for f in seed_fields:
        fid = f"field:{f['model_field']}"
        if fid in graph.nodes:
            seeds.add(fid)

    if not seeds:
        return {"status": "no_seeds", "layers": [], "nodes": {}, "total_visited": 0}

    return graph.bfs_expand(seeds, depth=depth)


def graph_paths_between_seed_types(
    graph: CodeGraph,
    seed_screens: list[dict],
    seed_fields: list[dict],
) -> list[dict[str, Any]]:
    """Find shortest paths from screen nodes to field nodes."""
    paths: list[dict] = []
    for s in seed_screens:
        sid = f"screen:{s['screen_name']}"
        for f in seed_fields:
            fid = f"field:{f['model_field']}"
            path = graph.trace_path(sid, fid)
            if path:
                paths.append({"from": sid, "to": fid, "path": path, "length": len(path)})
    return sorted(paths, key=lambda p: p["length"])[:5]


def build_graph_evidence_pack(
    index: AppIndex,
    screen_candidates: list[dict],
    condition_hits: list[dict],
    api_hits: list[dict],
    field_hits: list[dict],
    max_paths: int = 3,
) -> dict[str, Any]:
    """Build graph-traversal evidence pack for MCP response."""
    graph = build_code_graph(index)

    expansion = graph_expand_from_seeds(
        graph, screen_candidates, condition_hits, api_hits, field_hits, depth=2
    )

    paths = graph_paths_between_seed_types(graph, screen_candidates, field_hits)

    clusters = graph.feature_clusters()
    matched_screen_names = {s['screen_name'] for s in screen_candidates}
    relevant_clusters = [
        c for c in clusters
        if any(sname in matched_screen_names for sname in c.get("screens", []))
    ]

    return {
        "graph_summary": {
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "connected_components": len(clusters),
        },
        "graph_expansion": {
            "seed_count": expansion.get("total_visited", 0),
            "depth_reached": len(expansion.get("layers", [])),
            "layers": [
                {"hop": i + 1, "edges": layer}
                for i, layer in enumerate(expansion.get("layers", []))
            ][:3],
            "related_nodes": dict(list(expansion.get("nodes", {}).items())[:30]),
        },
        "graph_paths": {
            "screen_to_field_paths": paths[:max_paths],
            "path_count": len(paths),
        },
        "feature_clusters": relevant_clusters[:5],
        "god_nodes": graph.god_nodes(top_n=5),
    }
