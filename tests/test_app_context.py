from pathlib import Path

from app_context_mcp.context import ask_app_context
from app_context_mcp.indexer import build_index

FIXTURE = Path(__file__).parent / "fixtures" / "flutter_app"


def test_index_extracts_screen_api_fields_and_conditions():
    index = build_index(FIXTURE)

    assert "OrderDetailScreen" in index.screens
    screen = index.screens["OrderDetailScreen"]
    assert screen.route == "/orders/:id"
    assert "Chi tiết đơn hàng" in screen.visible_texts
    assert "Hủy đơn" in screen.visible_texts

    assert any(api.method == "GET" and api.path_template == "/orders/{id}" for api in index.api_calls)
    assert any(api.method == "POST" and api.path_template == "/orders/{id}/cancel" for api in index.api_calls)

    assert "OrderDetailModel.canCancel" in index.fields
    assert "OrderDetailModel.status" in index.fields
    assert any("canCancel" in cond.expression and "shipping" in cond.expression for cond in index.conditions)


def test_ask_app_context_returns_rich_evidence_pack_for_cancel_order():
    result = ask_app_context(
        repo_path=FIXTURE,
        question="Màn chi tiết đơn hàng vì sao không hiện nút Hủy đơn? API field nào quyết định?",
        screenshot_text="Chi tiết đơn hàng Hủy đơn Đang giao",
    )

    assert result["status"] in {"found", "partial"}
    assert result["query_interpretation"]["role_assumption"] == "NON_TECH"
    assert result["screen_candidates"][0]["screen_name"] == "OrderDetailScreen"
    assert any(api["method"] == "GET" and api["path"] == "/orders/{id}" for api in result["api_usage"])
    assert any(field["raw_json_field"] == "canCancel" for field in result["api_field_lineage"])
    assert any("canCancel" in fact["fact"] for fact in result["current_logic_facts"])
    assert result["evidence_registry"]
    assert all("id" in ev and "file" in ev and "snippet" in ev for ev in result["evidence_registry"])
    assert result["index_info"]["sources_indexed"]["dart_files"] >= 4


def test_ask_app_context_surfaces_api_workaround_gap():
    result = ask_app_context(
        repo_path=FIXTURE,
        question="Nếu muốn hiện lý do vì sao không hủy được đơn thì API cần workaround như nào?",
        screenshot_text="Chi tiết đơn hàng Hủy đơn",
        desired_behavior="Show reason why order cannot be cancelled",
    )

    analysis = result["desired_behavior_analysis"]
    assert analysis["current_support"]["status"] in {"not_supported", "partially_supported", "unknown"}
    assert any("cancelBlockedReason" in gap.get("missing_data", []) for gap in analysis["current_gaps"])
    assert any(opt["option_type"] in {"add_response_field", "backend_driven_actions"} for opt in analysis["api_options"])
    assert result["socratic_followups"]


def test_not_found_does_not_hallucinate_specific_screen():
    result = ask_app_context(
        repo_path=FIXTURE,
        question="Màn ví thưởng loyalty spin wheel logic ra sao?",
        screenshot_text="Vòng quay may mắn điểm thưởng",
    )

    assert result["status"] in {"not_found", "partial", "ambiguous"}
    assert result["unknowns_and_limits"]
    assert not any(candidate["screen_name"] == "LoyaltySpinWheelScreen" for candidate in result["screen_candidates"])


def test_execution_flow_trace_chain():
    """Verify full Screen → Widget → Hook → Service → API chain in execution flows."""
    result = ask_app_context(
        repo_path=FIXTURE,
        question="Chi tiết đơn hàng",
    )
    assert result["status"] == "found"
    assert result["screen_candidates"][0]["screen_name"] == "OrderDetailScreen"

    # Execution flows must contain the chain
    flows = result["execution_flows"]
    assert flows, "Should have at least one execution flow"
    flow = flows[0]
    steps = flow["steps"]
    kinds = [s["kind"] for s in steps]
    assert "screen" in kinds
    assert "ui_widget" in kinds, "Should have widget step"
    assert "ui_hook" in kinds, "Should have hook step"
    assert any(k in kinds for k in ("service_call", "repo_call", "api_call")), "Should trace to service/api"
    assert any(api == "/orders/{id}/cancel" for api in flow["apis_involved"]), "Should include cancel API"


def test_code_graph_trace_edges():
    """Verify code graph edges follow Screen → Widget → Hook → API chain."""
    from app_context_mcp.indexer import build_index
    from app_context_mcp.graph_builder import build_graph
    from app_context_mcp.code_graph import build_code_graph

    index = build_index(FIXTURE)
    build_graph(index)
    cg = build_code_graph(index)

    relations = {e[2] for e in cg.edges}
    assert "contains" in relations, "Screen→Widget edge missing"
    assert "triggers_hook" in relations, "Widget→Hook edge missing"
    assert "calls_api" in relations, "Hook/API→API edge missing"

    # Find trace path
    screen = "screen:OrderDetailScreen"
    widget = None
    hook = None
    api_target = None
    for e in cg.edges:
        if e[0] == screen and e[2] == "contains":
            widget = e[1]
        if e[0].startswith("screen:") and e[2] == "triggers":
            hook = e[1]
        if e[2] == "triggers_hook":
            widget = e[0]
            hook = e[1]
        if e[2] == "calls_api" and e[0].startswith("hook:"):
            hook = e[0]
            api_target = e[1]

    assert widget and widget.startswith("widget:"), "Widget node not found"
    assert hook and hook.startswith("hook:"), "Hook node not found"
    assert api_target and api_target.startswith("api:"), "API endpoint not linked from hook"
