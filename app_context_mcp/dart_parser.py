from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tree_sitter import Node, Parser, Tree

logger = logging.getLogger(__name__)

# ── Lazy imports ─────────────────────────────────────────────────────────

_tree_sitter_enabled = False


def _ensure_tree_sitter() -> bool:
    """Attempt to import tree-sitter + dart language once, return True if available."""
    global _tree_sitter_enabled
    if _tree_sitter_enabled:
        return True
    try:
        from tree_sitter import Language, Parser
        from tree_sitter_languages import get_language
        _tree_sitter_enabled = True
        return True
    except Exception:
        return False


def _get_language() -> Any:
    from tree_sitter_languages import get_language
    return get_language("dart")


def _new_parser() -> Any:
    from tree_sitter import Parser
    return Parser(_get_language())


# ── Tree-sitter capture queries (Dart grammar) ─────────────────────────────

DART_QUERIES: dict[str, str] = {
    "class": r"""
(class_definition
  name: (identifier) @name) @cls
""",
    "enum": r"""
(enum_declaration
  name: (identifier) @name) @decl
""",
    "mixin": r"""
(mixin_declaration
  name: (identifier) @name) @decl
""",
    "superclass": r"""
(class_definition
  superclass: (superclass
    type: (type_identifier) @super)) @decl
""",
    "interfaces": r"""
(class_definition
  interfaces: (interfaces
    (type_identifier) @iface)
) @decl
""",
    "build_method": r"""
(method_signature
  name: (identifier) @name (#eq? @name "build")
) @decl
""",
    "method": r"""
(function_signature | method_signature
  name: (identifier) @name) @decl
""",
    "static_route": r"""
(static_final_declaration
  (variable_declaration
    name: (identifier) @var
    value: (string_literal
      (string_content) @path))) @decl
""",
    "client_call": r"""
(method_invocation
  target: (identifier) @target (#eq? @target "client")
  method_name: (identifier) @http (#match? @http "^(get|post|put|delete|patch)$")
  arguments: (arguments
          . (string_literal (string_content) @path))
) @call
""",
    "text_widget": r"""
(method_invocation
  method_name: (identifier) @fn (#eq? @fn "Text")
  arguments: (arguments
          . (string_literal (string_content) @text))
) @decl
""",
    "import": r"""
(import_or_export
  (string_literal (string_content) @path)) @decl
""",
}


# ── Helper utilities ─────────────────────────────────────────────────────


class _Capture:
    """Wrap tree-sitter captures for easier access."""

    def __init__(self, caps: list[tuple["Node", str]]) -> None:
        self.caps = caps

    def by_name(self, *names: str) -> list["Node"]:
        return [node for node, n in self.caps if n in names]

    def first(self, *names: str) -> "Node | None":
        for node, n in self.caps:
            if n in names:
                return node
        return None

    def grouped_by(self, group: str) -> list[list[tuple["Node", str]]]:
        """Group captures by their top-level @decl / @call node."""
        groups: list[list[tuple["Node", str]]] = []
        cur: list[tuple["Node", str]] = []
        for node, name in self.caps:
            if name.endswith((".decl", ".call", ".cls")):
                if cur:
                    groups.append(cur)
                cur = []
            else:
                if not cur and groups:
                    groups[-1].append((node, name))
                else:
                    cur.append((node, name))
        return groups


def _node_text(node: "Node") -> str:
    t = node.text
    return t.decode() if isinstance(t, bytes) else str(t) if t else ""


def _containing_class(node: "Node") -> str:
    n = node
    while n and n.type != "class_definition":
        n = n.parent
    if not n:
        return ""
    for c in n.children:
        if c.type == "identifier":
            return _node_text(c)
    return ""


def _nearest_function(node: "Node") -> str:
    n = node
    while n and n.type not in ("method_signature", "function_signature", "function_declaration", "method_declaration"):
        n = n.parent
    if not n:
        return ""
    for c in n.children:
        if c.type == "identifier":
            return _node_text(c)
    return ""


# ── Dart AST interface ─────────────────────────────────────────────────


class DartParser:
    """Tree-sitter Dart AST parser — gracefully optional (no hard dependency)."""

    _parser: "Parser | None" = None

    def __init__(self) -> None:
        if not _ensure_tree_sitter():
            raise RuntimeError("tree-sitter & tree-sitter-languages required")
        if DartParser._parser is None:
            DartParser._parser = _new_parser()

    # ── Core parse ────────────────────────────────────────────────────────

    def parse(self, source: str | bytes) -> "Tree":
        data = source.encode() if isinstance(source, str) else source
        return self._parser.parse(data)  # type: ignore[union-attr]

    def parse_file(self, path: Path) -> "Tree":
        return self.parse(path.read_bytes())

    # ── Query runner ─────────────────────────────────────────────────────

    def _run_query(self, tree: "Tree", name: str) -> _Capture:
        lang = _get_language()
        q = lang.query(DART_QUERIES[name])
        return _Capture(q.captures(tree.root_node))

    # ── Extractors ──────────────────────────────────────────────────────

    def classes(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "class")
        return [{"name": _node_text(n), "line": n.start_point[0] + 1} for n in caps.by_name("name")]

    def superclasses(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "superclass")
        results: list[dict] = []
        name = ""
        line = 0
        for node, cname in caps.caps:
            if cname == "super":
                results.append({"class": name, "super": _node_text(node), "line": line})
            elif cname == "name":
                name = _node_text(node)
                line = node.start_point[0] + 1
        return results

    def enums(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "enum")
        return [{"name": _node_text(n), "line": n.start_point[0] + 1} for n in caps.by_name("name")]

    def mixins(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "mixin")
        return [{"name": _node_text(n), "line": n.start_point[0] + 1} for n in caps.by_name("name")]

    def methods(self, tree: Tree) -> list[dict]:
        caps = self._run_query(tree, "method")
        results: list[dict] = []
        for node, cname in caps.caps:
            if cname == "name":
                results.append({
                    "name": _node_text(node),
                    "class_scope": _containing_class(node),
                    "line": node.start_point[0] + 1,
                    "is_build": _node_text(node) == "build",
                })
        return results

    def imports(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "import")
        return [{"path": _node_text(n), "line": n.start_point[0] + 1, "is_package": _node_text(n).startswith("package:")} for n in caps.by_name("path")]

    def routes(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "static_route")
        results: list[dict] = []
        var_name = ""
        line = 0
        for node, cname in caps.caps:
            if cname == "var":
                var_name = _node_text(node)
                line = node.start_point[0] + 1
            elif cname == "path":
                results.append({"var_name": var_name, "path": _node_text(node), "line": line})
        return results

    def http_calls(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "client_call")
        results: list[dict] = []
        method = path = ""
        line = 0
        for node, cname in caps.caps:
            if cname == "http":
                method = _node_text(node).upper()
                line = node.start_point[0] + 1
            elif cname == "path":
                path = _node_text(node)
            elif cname == "call":
                results.append({
                    "method": method,
                    "path": path,
                    "enclosing_method": _nearest_function(node),
                    "line": line,
                })
        return results

    def text_widgets(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "text_widget")
        return [{"text": _node_text(n), "line": n.start_point[0] + 1} for n in caps.by_name("text")]

    def build_methods(self, tree: "Tree") -> list[dict]:
        caps = self._run_query(tree, "build_method")
        return [{"name": _node_text(n), "class_scope": _containing_class(n), "line": n.start_point[0] + 1} for n in caps.by_name("name")]

    # ── High-level classification ───────────────────────────────────────

    def classify(self, tree: "Tree") -> dict[str, Any]:
        """Full analysis returning all extractions."""
        classes = self.classes(tree)
        methods = self.methods(tree)
        names = [c["name"] for c in classes]
        return {
            "classes": classes,
            "enums": self.enums(tree),
            "mixins": self.mixins(tree),
            "methods": methods,
            "imports": self.imports(tree),
            "routes": self.routes(tree),
            "http_calls": self.http_calls(tree),
            "text_widgets": self.text_widgets(tree),
            "build_methods": self.build_methods(tree),
            "is_screen": any(n.endswith(("Screen", "Page", "View")) for n in names),
            "is_model": any(n.endswith(("Model", "Entity", "DTO")) for n in names),
            "is_widget": any(m["is_build"] for m in methods),
            "is_repo": any(n.endswith(("Repository", "Repo", "Provider", "DataSource")) for n in names),
            "is_bloc": any(n.endswith(("Bloc", "Cubit", "ViewModel", "Controller")) for n in names),
            "is_service": any(n.endswith(("Service", "Client", "Helper", "Util")) for n in names),
        }
