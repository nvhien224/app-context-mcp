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
