from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import AppIndex


@dataclass
class CodeGraphNode:
    node_id: str
    node_type: str  # screen | api | model | field | condition | file | method
    label: str
    file: str | None
    lines: str | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeGraph:
    """Lightweight knowledge graph extracted from AppIndex."""

    nodes: dict[str, CodeGraphNode] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)

    def add_node(self, node_id: str, node_type: str, label: str, file: str | None = None, lines: str | None = None, **kwargs) -> None:
        if node_id not in self.nodes:
            self.nodes[node_id] = CodeGraphNode(node_id, node_type, label, file, lines, extra=kwargs)

    def add_edge(self, from_id: str, to_id: str, relation: str) -> None:
        edge = (from_id, to_id, relation)
        if edge not in self.edges:
            self.edges.append(edge)

    def neighbors(self, node_id: str) -> list[tuple[str, str]]:
        return [(to_id, rel) for frm, to_id, rel in self.edges if frm == node_id]

    def predecessors(self, node_id: str) -> list[tuple[str, str]]:
        return [(frm, rel) for frm, to_id, rel in self.edges if to_id == node_id]


    def bfs_expand(self, seed_nodes: set[str], depth: int = 2) -> dict[str, Any]:
        """Graph-traverse from seed nodes — BFS expansion with relation tracking."""
        from collections import deque
        visited: set[str] = set()
        frontier = set(seed_nodes)
        layers: list[list[dict]] = []
        for _ in range(depth):
            if not frontier:
                break
            next_frontier: set[str] = set()
            layer_hits = []
            for node in frontier:
                if node in visited:
                    continue
                visited.add(node)
                for to, rel in self.neighbors(node):
                    next_frontier.add(to)
                    layer_hits.append({"from": node, "to": to, "relation": rel})
                for frm, rel in self.predecessors(node):
                    next_frontier.add(frm)
                    layer_hits.append({"from": frm, "to": node, "relation": rel})
            layers.append(layer_hits)
            frontier = next_frontier - visited
        node_snapshots = {nid: {"id": self.nodes[nid].node_id, "type": self.nodes[nid].node_type, "label": self.nodes[nid].label} for nid in visited if nid in self.nodes}
        return {"layers": layers, "nodes": node_snapshots, "total_visited": len(visited)}

    def trace_path(self, start_id: str, end_id: str) -> list[dict] | None:
        """Shortest path (BFS) from start to end node."""
        if start_id not in self.nodes or end_id not in self.nodes:
            return None
        from collections import deque
        queue = deque([[start_id]])
        seen = {start_id}
        while queue:
            path = queue.popleft()
            last = path[-1]
            if last == end_id:
                return [{"node": self.nodes[n].label, "id": n} for n in path]
            for to, rel in self.neighbors(last):
                if to not in seen:
                    seen.add(to)
                    queue.append(path + [to])
        return None

    def feature_clusters(self) -> list[dict[str, Any]]:
        """Group connected components into feature-like clusters."""
        comps = self.connected_components()
        clusters = []
        for i, comp in enumerate(comps[:30]):
            screens = sorted([self.nodes[n].label for n in comp if n.startswith("screen:")])
            apis = sorted({self.nodes[n].label for n in comp if n.startswith("api:")})
            fields = sorted({self.nodes[n].extra.get("raw_json_field", self.nodes[n].label) for n in comp if n.startswith("field:")})
            conditions = sorted({self.nodes[n].label for n in comp if n.startswith("condition:")})
            label = screens[0] if screens else (conditions[0] if conditions else f"cluster_{i}")
            clusters.append({
                "id": f"cluster_{i:03d}",
                "label": label,
                "node_count": len(comp),
                "screens": screens[:5],
                "apis": apis[:5],
                "fields": fields[:5],
                "conditions": conditions[:5],
            })
        return clusters
    def connected_components(self) -> list[set[str]]:
        adj: dict[str, set[str]] = defaultdict(set)
        for frm, to_id, _ in self.edges:
            adj[frm].add(to_id)
            adj[to_id].add(frm)
        visited: set[str] = set()
        components: list[set[str]] = []
        for node in self.nodes:
            if node in visited:
                continue
            stack = [node]
            comp: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                comp.add(cur)
                for nxt in adj[cur]:
                    if nxt not in visited:
                        stack.append(nxt)
            components.append(comp)
        return components

    def god_nodes(self, top_n: int = 10) -> list[dict[str, Any]]:
        deg: dict[str, int] = defaultdict(int)
        for frm, to_id, _ in self.edges:
            deg[frm] += 1
            deg[to_id] += 1
        ranked = sorted(deg.items(), key=lambda x: -x[1])[:top_n]
        return [{"node_id": nid, "degree": d, **self.nodes[nid].extra} for nid, d in ranked if nid in self.nodes]

    def export(self) -> dict[str, Any]:
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "nodes": [
                {
                    "id": n.node_id,
                    "type": n.node_type,
                    "label": n.label,
                    "file": n.file,
                    "lines": n.lines,
                    **n.extra,
                }
                for n in self.nodes.values()
            ],
            "edges": [{"from": frm, "to": to_id, "relation": rel} for frm, to_id, rel in self.edges],
            "connected_components": [list(c) for c in self.connected_components()],
            "god_nodes": self.god_nodes(top_n=10),
        }


def build_code_graph(index: AppIndex) -> CodeGraph:
    """Build a knowledge graph from parsed AppIndex."""
    g = CodeGraph()

    # Screen nodes
    for name, screen in index.screens.items():
        sid = f"screen:{name}"
        g.add_node(sid, "screen", name, file=screen.file, visible_texts=screen.visible_texts, route=screen.route)
        g.add_edge(sid, f"file:{screen.file}", "declared_in")

    # API nodes
    for api in index.api_calls:
        aid = f"api:{api.method} {api.path_template}"
        g.add_node(aid, "api", f"{api.method} {api.path_template}", file=api.file, lines=str(api.line), caller=api.client_method)
        g.add_edge(aid, f"file:{api.file}", "declared_in")
        if api.client_method:
            mid = f"method:{api.client_method}"
            g.add_node(mid, "method", api.client_method, file=api.file)
            g.add_edge(aid, mid, "called_from")

    # Field/Model nodes
    for qual_name, field in index.fields.items():
        fid = f"field:{qual_name}"
        g.add_node(fid, "field", field.field, file=field.file, lines=str(field.line), type_hint=field.type_hint, raw_json_field=field.raw_json_field)
        mid = f"model:{field.owner}"
        g.add_node(mid, "model", field.owner, file=field.file)
        g.add_edge(fid, mid, "belongs_to")
        g.add_edge(fid, f"file:{field.file}", "declared_in")

    # Condition nodes + dependencies + screen links
    for cond in index.conditions:
        cid = f"condition:{cond.file}:{cond.line}"
        g.add_node(cid, "condition", cond.expression[:80], file=cond.file, lines=str(cond.line), target_widget=cond.target, dependent_fields=cond.fields)
        g.add_edge(cid, f"file:{cond.file}", "declared_in")
        for f in cond.fields:
            # Match field by qualified or plain name
            matched = False
            for qual_name in index.fields:
                if f in qual_name or f == qual_name:
                    g.add_edge(cid, f"field:{qual_name}", "depends_on")
                    matched = True
                    break
            if not matched:
                # dangling field node
                g.add_node(f"unresolved_field:{f}", "field", f)
                g.add_edge(cid, f"unresolved_field:{f}", "depends_on")
        # Link to screen in same file
        for name, screen in index.screens.items():
            if screen.file == cond.file:
                g.add_edge(f"screen:{name}", cid, "renders")
                break

    # Cross edges: screen->api via file co-occur
    for name, screen in index.screens.items():
        sid = f"screen:{name}"
        for api in index.api_calls:
            if api.file == screen.file:
                aid = f"api:{api.method} {api.path_template}"
                g.add_edge(sid, aid, "data_source")

    # Cross edges: api->model via caller hint
    for api in index.api_calls:
        if not api.client_method:
            continue
        aid = f"api:{api.method} {api.path_template}"
        for qual_name, field in index.fields.items():
            if field.owner and field.owner.lower() in api.client_method.lower():
                mid = f"model:{field.owner}"
                if mid in g.nodes:
                    g.add_edge(aid, mid, "uses")

    # Field betweenness: link fields referenced in conditions of same screen
    screen_to_conditions: dict[str, list[str]] = defaultdict(list)
    for cond in index.conditions:
        for name, screen in index.screens.items():
            if screen.file == cond.file or screen.file == cond.file:
                screen_to_conditions[name].append(cond.expression)

    # Co-reference edges: conditions that share fields
    condition_field_nodes: dict[str, set[str]] = defaultdict(set)
    for cond in index.conditions:
        cid = f"condition:{cond.file}:{cond.line}"
        for f in cond.fields:
            for qual_name in index.fields:
                if f in qual_name or f == qual_name:
                    condition_field_nodes[cid].add(f"field:{qual_name}")
                    break
    for cond1, fields1 in condition_field_nodes.items():
        for cond2, fields2 in condition_field_nodes.items():
            if cond1 >= cond2:
                continue
            shared = fields1 & fields2
            for fn in shared:
                g.add_edge(cond1, cond2, "co_conditions_via" if fn == list(shared)[0] else "co_condition")

    # Widget nodes + screen→widget edges
    for wid, w in index.widgets.items():
        g.add_node(wid, "widget", w.widget_type, file=w.file, lines=str(w.line), condition=w.condition_expression)
        if w.enclosing_screen:
            g.add_edge(f"screen:{w.enclosing_screen}", wid, "contains")

    # Hook nodes + screen→hook edges + hook→API/method edges
    for i, h in enumerate(index.hooks):
        hid = f"hook:{h.file}:{h.line}:{i}"
        g.add_node(hid, "hook", f"{h.hook_type}: {h.handler_method}", file=h.file, lines=str(h.line))
        if h.enclosing_screen:
            screen_id = f"screen:{h.enclosing_screen}"
            g.add_edge(screen_id, hid, "triggers")
            # Extract method name from handler like "cubit.cancelOrder(order.id)"
            handler = h.handler_method
            if '(' in handler:
                handler = handler.split('(')[0]
            if '.' in handler:
                handler = handler.split('.')[-1]
            handler = handler.strip()
            for api in index.api_calls:
                if api.client_method == handler:
                    aid = f"api:{api.method} {api.path_template}"
                    g.add_edge(hid, aid, "calls_api")
            # Link hook → cubit/bloc method
            for sid, sym in index.symbols.items():
                if sym.kind == "method" and sym.name == handler:
                    g.add_edge(hid, sid, "invokes")

    # Widget→Hook linkage: link hooks contained within widgets in same screen
    for wid, w in index.widgets.items():
        if w.enclosing_screen:
            screen_id = f"screen:{w.enclosing_screen}"
            for i, h in enumerate(index.hooks):
                if h.enclosing_screen == w.enclosing_screen and h.file == w.file:
                    hid = f"hook:{h.file}:{h.line}:{i}"
                    # Link widget→hook if hook comes after widget line
                    if h.line >= w.line:
                        g.add_edge(wid, hid, "triggers_hook")

    # Service (Method) → API edges: client method in API calls
    for api in index.api_calls:
        if api.client_method:
            aid = f"api:{api.method} {api.path_template}"
            mid = f"method:{api.client_method}"
            if mid in g.nodes:
                g.add_edge(mid, aid, "calls_api")

    return g
