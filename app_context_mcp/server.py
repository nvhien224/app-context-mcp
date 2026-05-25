from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .context import ask_app_context


# ── MCP Call Logging ─────────────────────────────────────────────────────────
# Arkon pattern: track latency, status, query text, repo name
# Store to ~/.hermes/agent-memory/app-context-mcp-calls.log

_LOG_DIR = Path.home() / ".hermes" / "agent-memory"
_LOG_FILE = _LOG_DIR / "app-context-mcp-calls.jsonl"


def _log_call(
    tool_name: str,
    repo_path: str,
    question: str,
    latency_ms: float,
    status: str,
    error: str | None,
) -> None:
    """Fire-and-forget log entry. Never blocks."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool_name,
            "repo": str(Path(repo_path).resolve()),
            "q": question[:200],
            "latency_ms": round(latency_ms, 2),
            "status": status,
            "error": (error[:200] if error else None),
        }
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never crash on logging


def create_server(host: str = "127.0.0.1", port: int = 8000) -> Any:
    """Create the MCP server with rich instructions + resource + logging."""
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:
        raise RuntimeError("MCP package with FastMCP is required. Install with `uv sync` or `pip install mcp`.") from exc

    mcp = FastMCP(
        "app-context-mcp",
        host=host,
        port=port,
        sse_path="/sse",
        message_path="/messages/",
        instructions=(
            "Bạn đang kết nối tới **App Context MCP** — server trace Flutter codebase thông qua "
            "Semble hybrid search, CodeGraph knowledge graph, Dart static analyzer, "
            "và execution flow tracer.\n\n"
            "## CÁCH DÙNG\n"
            "1. **Luôn truy vấn app-context-mcp trước** khi trả lời câu hỏi liên quan đến:\n"
            "   - Màn hình (screen)/page/route, UI elements\n"
            "   - Logic hiện tại: ẩn/hiện nút, điều kiện, trạng thái\n"
            "   - API endpoint: method, path, caller\n"
            "   - API field: tên JSON, type hint, model mapping\n"
            "   - Workaround/đề xuất thay đổi business logic\n"
            "   - Execution flow: đường đi từ UI → State → API → Response\n\n"
            "2. **Thứ tự trace**:\n"
            "   - `ask_app_context_tool` → evidence pack\n"
            "   - Đọc `screen_candidates`, `code_trace_graph`, `api_field_lineage`, `execution_flows`\n"
            "   - Kiểm tra `import_graph` để hiểu dependency giữa files\n"
            "   - Kiểm tra `unknowns_and_limits` để biết gì chưa biết\n"
            "   - Đọc `evidence_registry` cho file/line chính xác\n\n"
            "3. **Screenshot text**: nếu user gửi screenshot → truyền text OCR vào `screenshot_text`.\n"
            "   Server sẽ match chữ Việt/Anh qua Semble semantic search.\n\n"
            "## Citation\n"
            "Luôn dẫn chứng file + line number từ `evidence_registry` khi trả lời.\n"
            "Không suy diễn từ knowledge chung — chỉ dùng evidence từ code thật.\n\n"
            "## Limitations\n"
            "- Regex-based Dart parser với Tree-sitter fallback (đang cải tiến).\n"
            "- Semble may fail on large repo; fallback to full scan.\n"
            "- Execution flows là heuristic (best-effort), không 100% chính xác.\n"
            "- No runtime code execution; only static code analysis.\n"
        ),
    )

    # ── MCP Resources ──
    # Arkon pattern: expose static/semi-static data as resources
    # Client reads at session start without calling tool

    @mcp.resource("app-context://about")
    async def about_resource() -> str:
        """About MCP capabilities and usage guide."""
        return (
            "# App Context MCP\n\n"
            "**One-tool server** expose `ask_app_context_tool`.\n\n"
            "**Stack**: Semble (semantic+BM25) → Dart static analyzer → CodeGraph → evidence pack\n\n"
            "**Output với mỗi query**:\n"
            "- `status`: found / partial / missing\n"
            "- `screen_candidates`: màn hình match query\n"
            "- `code_trace_graph`: screen → api → field → condition\n"
            "- `api_field_lineage`: field map JSON ↔ Dart\n"
            "- `current_logic_facts`: logic hiện tại\n"
            "- `five_w_one_h`: Who/What/When/Where/Why/How\n"
            "- `evidence_registry`: file + line + snippet\n"
            "- `unknowns_and_limits`: gì chưa biết\n"
            "- `code_graph`: graph nodes + edges + god_nodes\n"
            "- `search_info`: Semble candidate files + query\n\n"
            "**Screenshot text**: đưa text OCR → server match bằng embedding.\n\n"
            "**Citation**: luôn dẫn file:line từ evidence_registry.\n"
        )

    @mcp.resource("app-context://logs")
    async def logs_resource() -> str:
        """Recent tool calls log (last 50)."""
        if not _LOG_FILE.exists():
            return "No calls yet."
        lines = _LOG_FILE.read_text(encoding="utf-8").strip().splitlines()[-50:]
        entries = [json.loads(line) for line in lines if line.strip()]
        return json.dumps(
            {
                "total_calls": len(entries),
                "recent_calls": entries,
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── MCP Tool ──
    @mcp.tool()
    def ask_app_context_tool(
        repo_path: str,
        question: str,
        screenshot_text: str | None = None,
        user_role: str | None = None,
        desired_behavior: str | None = None,
        max_results: int = 20,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Return a structured evidence/context pack about a Flutter codebase.

        Truy vấn tuyệt đối cho: màn hình Flutter, logic ẩn/hiện nút,
        API endpoint/field, condition tại sao UI disable, workaround cho BE.
        """
        # ── Strict input validation ──
        repo_p = Path(repo_path)
        if not repo_p.exists() or not repo_p.is_dir():
            return {
                "status": "error",
                "error": f"repo_path '{repo_path}' does not exist or is not a directory.",
            }
        if not isinstance(question, str) or not question.strip():
            return {
                "status": "error",
                "error": "question must be a non-empty string.",
            }
        if not isinstance(max_results, int) or max_results < 1:
            max_results = 10
        elif max_results > 200:
            max_results = 200  # hard cap

        q = question.strip()[:2000]
        t0 = time.perf_counter()
        status = "ok"
        error = None
        result: dict[str, Any] = {}
        try:
            result = ask_app_context(
                repo_path=repo_p,
                question=q,
                screenshot_text=screenshot_text,
                user_role=user_role,
                desired_behavior=desired_behavior,
                max_results=max_results,
                token_budget=token_budget,
            )
            status = result.get("status", "ok")
        except Exception as exc:
            status = "error"
            error = str(exc)
            result = {"status": "error", "error": error}

        latency_ms = (time.perf_counter() - t0) * 1000
        _log_call("ask_app_context_tool", str(repo_p), q, latency_ms, status, error)
        return result

    return mcp


def _wrap_streamable_http_app(mcp_app: Any) -> Any:
    """Deprecated: kept for compatibility but not used at runtime.

    FastMCP streamable_http_app() requires lifespan init từ .run().
    Khi mount qua uvicorn trước khi .run() gọi = Task group not initialized.
    Claude Desktop chỉ hỗ trợ SSE (/sse) — không hỗ trợ streamable-http.
    """
    return mcp_app  # passthrough — không mount wrapper gây lỗi


def _log_transport_switch(requested: str, actual: str) -> None:
    import logging
    logging.getLogger("app-context-mcp").warning(
        f"Transport '{requested}' không khả thi với uvicorn lifespan — auto-switch sang '{actual}'. "
        f"Claude Desktop chỉ hỗ trợ SSE (/sse). URL: https://host:port/sse"
    )


def run_https_server(
    server: Any,
    transport: str,
    host: str,
    port: int,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    generate_self_signed: bool,
) -> None:
    """FastMCP SSE/HTTP with uvicorn TLS."""
    if transport == "stdio":
        raise SystemExit("HTTPS is only valid for sse or streamable-http transports, not stdio.")

    # Issue #8: streamable_http_app() chưa init task group khi mount → crash.
    # Claude Desktop chỉ dùng SSE (/sse) — auto-switch.
    if transport == "streamable-http":
        _log_transport_switch("streamable-http", "sse")
        transport = "sse"

    certfile = ssl_certfile
    keyfile = ssl_keyfile
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if generate_self_signed:
        temp_dir = tempfile.TemporaryDirectory(prefix="app-context-mcp-tls-")
        certfile = str(Path(temp_dir.name) / "cert.pem")
        keyfile = str(Path(temp_dir.name) / "key.pem")
        generate_dev_certificate(certfile, keyfile)
        print(f"Generated temporary self-signed certificate: {certfile}")

    if not certfile or not keyfile:
        raise SystemExit("HTTPS requires --ssl-certfile and --ssl-keyfile, or --generate-self-signed.")

    import uvicorn

    # Chỉ dùng sse_app() — streamable_http_app() không hoạt động với uvicorn TLS
    app = server.sse_app()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )

    if temp_dir is not None:
        temp_dir.cleanup()


def _wrap_http_run(server: Any, transport: str, host: str, port: int) -> None:
    """Auto-switch streamable-http → SSE (issue #8: task group not initialized)."""
    if transport == "streamable-http":
        _log_transport_switch("streamable-http", "sse")
        transport = "sse"
    server.run(transport=transport, host=host, port=port)


def generate_dev_certificate(certfile: str, keyfile: str) -> None:
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", keyfile,
        "-out", certfile,
        "-days", "7",
        "-subj", "/CN=app-context-mcp.local",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    try:
        subprocess.check_call(cmd)
    except FileNotFoundError as exc:
        raise SystemExit("openssl is required for --generate-self-signed.") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="App Context MCP server / CLI")
    parser.add_argument("--repo", help="Repo path for one-shot CLI mode")
    parser.add_argument("--question", help="Question for one-shot CLI mode")
    parser.add_argument("--screenshot-text", default=None)
    parser.add_argument("--user-role", default=None)
    parser.add_argument("--desired-behavior", default=None)
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="sse")
    parser.add_argument("--host", default="0.0.0.0", help="Host bind for SSE/HTTP. Default: 0.0.0.0 for LAN.")
    parser.add_argument("--port", type=int, default=8000, help="Port. Default: 8000.")
    parser.add_argument("--https", action="store_true", help="Serve SSE/HTTP over HTTPS via uvicorn TLS.")
    parser.add_argument("--ssl-certfile", default=None, help="TLS cert file.")
    parser.add_argument("--ssl-keyfile", default=None, help="TLS key file.")
    parser.add_argument("--generate-self-signed", action="store_true", help="Generate self-signed cert for HTTPS dev.")
    args = parser.parse_args()

    if args.repo and args.question:
        result = ask_app_context(
            repo_path=Path(args.repo),
            question=args.question,
            screenshot_text=args.screenshot_text,
            user_role=args.user_role,
            desired_behavior=args.desired_behavior,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    server = create_server(host=args.host, port=args.port)
    if args.https:
        run_https_server(
            server=server,
            transport=args.transport,
            host=args.host,
            port=args.port,
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
            generate_self_signed=args.generate_self_signed,
        )
    else:
        _wrap_http_run(server, args.transport, args.host, args.port)


if __name__ == "__main__":
    main()
