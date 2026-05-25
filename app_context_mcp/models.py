from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Evidence:
    id: str
    source_type: str
    file: str
    symbol: str
    lines: str
    snippet: str
    why_relevant: str


@dataclass
class ScreenInfo:
    name: str
    file: str
    route: str | None = None
    visible_texts: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ApiCall:
    method: str
    path_template: str
    client_method: str
    file: str
    line: int
    evidence_id: str


@dataclass
class FieldInfo:
    owner: str
    field: str
    raw_json_field: str
    type_hint: str | None
    file: str
    line: int
    evidence_id: str

    @property
    def qualified_name(self) -> str:
        return f"{self.owner}.{self.field}"


@dataclass
class ConditionInfo:
    expression: str
    target: str
    file: str
    line: int
    fields: list[str]
    evidence_id: str


@dataclass
class ImportEdge:
    """Cross-file import relationship."""
    source_file: str
    target_file: str
    import_path: str
    is_package: bool
    line: int


@dataclass
class SymbolNode:
    """Node in the code graph — class, method, enum, field."""
    id: str
    kind: str  # class | method | enum | field | variable | import
    name: str
    file: str
    line: int
    enclosing_class: str | None = None
    signature: str | None = None
    evidence_id: str | None = None


@dataclass
class SymbolEdge:
    """Edge in the code graph — calls, contains, references."""
    source: str  # SymbolNode.id
    target: str  # SymbolNode.id
    kind: str  # calls | contains | references | imports
    file: str
    line: int
    evidence_id: str | None = None


@dataclass
class ExecutionFlow:
    """End-to-end trace: UI → BLoC → Repo → API → Response."""
    name: str
    start_node: str  # SymbolNode.id
    end_node: str  # SymbolNode.id
    steps: list[dict] = field(default_factory=list)  # [{node, kind, file, line}]
    screens_involved: list[str] = field(default_factory=list)
    apis_involved: list[str] = field(default_factory=list)
    fields_involved: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class AppIndex:
    repo_path: Path
    screens: dict[str, ScreenInfo] = field(default_factory=dict)
    api_calls: list[ApiCall] = field(default_factory=list)
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    conditions: list[ConditionInfo] = field(default_factory=list)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    dart_files: int = 0
    # ── GitNexus-inspired graph additions ──
    imports: list[ImportEdge] = field(default_factory=list)
    symbols: dict[str, SymbolNode] = field(default_factory=dict)
    symbol_edges: list[SymbolEdge] = field(default_factory=list)
    execution_flows: list[ExecutionFlow] = field(default_factory=list)
    # file → set of imported files
    import_graph: dict[str, set[str]] = field(default_factory=dict)
    # reverse: file → set of files that import this file
    import_graph_reverse: dict[str, set[str]] = field(default_factory=dict)
    # hooks / annotations / models (lightweight)
    hooks: list[dict] = field(default_factory=list)
    annotations: list[dict] = field(default_factory=list)
    model_annotations: list[dict] = field(default_factory=list)
