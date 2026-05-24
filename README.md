# App Context MCP

One-tool MCP context/evidence server for Flutter codebases.

Goal: let Dev/PM/PO/BA/QC/BE use their own AI client (Claude, Cursor, Hermes, Gemini, etc.) to ask about current Flutter app behavior without making the AI client read the whole codebase.

The MCP server does **not** include an LLM layer. It returns a structured evidence pack: screens, flows, UI conditions, API calls, API fields, trace graph, confidence, unknowns, and follow-up questions. The caller's AI client synthesizes the final answer.

## Current MVP

- Python + FastMCP-compatible server
- Exposes one tool: `ask_app_context_tool`
- CLI one-shot mode for testing
- Static Dart/Flutter indexer for:
  - `Screen/Page/View` classes
  - static route strings
  - visible `Text(...)` labels
  - `client.get/post/put/delete/patch(...)` API calls
  - `json['field']` model mappings
  - simple UI conditions such as `Visibility(visible: ...)`, `enabled: ...`
- Structured JSON evidence pack
- No LLM inside server

## macOS setup

Install `uv` if needed:

```bash
brew install uv
# or: curl -LsSf https://astral.sh/uv/install.sh | sh
```

Run tests:

```bash
uv run --extra dev pytest -q
```

Run one-shot CLI:

```bash
uv run app-context-mcp \
  --repo /path/to/flutter_app \
  --question "Màn chi tiết đơn hàng vì sao không hiện nút Hủy? API field nào quyết định?" \
  --screenshot-text "Chi tiết đơn hàng Hủy đơn Đang giao"
```

Run as MCP HTTPS + SSE server for LAN AI clients:

```bash
uv run app-context-mcp \
  --transport sse \
  --https \
  --host 0.0.0.0 \
  --port 8443 \
  --ssl-certfile /path/to/cert.pem \
  --ssl-keyfile /path/to/key.pem
```

HTTPS SSE endpoint:

```txt
https://<your-mac-lan-ip>:8443/sse
```

Find your macOS LAN IP:

```bash
ipconfig getifaddr en0   # Wi-Fi
ipconfig getifaddr en1   # sometimes Ethernet/other adapter
```

For quick local/LAN development you can generate a temporary self-signed cert:

```bash
uv run app-context-mcp \
  --transport sse \
  --https \
  --generate-self-signed \
  --host 0.0.0.0 \
  --port 8443
```

Production/team LAN recommendation: use a trusted certificate via `mkcert`, Caddy, nginx, or a company CA. Many AI clients reject self-signed certificates unless the CA is trusted by macOS/keychain.

Example AI client config for HTTPS SSE-style clients:

```json
{
  "mcpServers": {
    "app-context": {
      "url": "https://<your-mac-lan-ip>:8443/sse"
    }
  }
}
```

If a client only supports stdio, run:

```bash
uv run app-context-mcp --transport stdio
```

## Tool contract

`ask_app_context_tool` inputs:

```json
{
  "repo_path": "/path/to/flutter_app",
  "question": "string",
  "screenshot_text": "optional string",
  "user_role": "optional; defaults NON_TECH",
  "desired_behavior": "optional string",
  "max_results": 20
}
```

Output shape:

```json
{
  "status": "found | partial | ambiguous | not_found",
  "query_interpretation": {},
  "screen_candidates": [],
  "case_flow_candidates": [],
  "code_trace_graph": {"nodes": [], "edges": []},
  "ui_elements": [],
  "api_usage": [],
  "api_field_lineage": [],
  "current_logic_facts": [],
  "desired_behavior_analysis": {},
  "five_w_one_h": {},
  "socratic_followups": [],
  "information_needed_from_user": [],
  "evidence_registry": [],
  "unknowns_and_limits": [],
  "index_info": {}
}
```

## Important limits

- Current MVP is static-code-only.
- No OCR image processing yet; pass `screenshot_text` from the AI client if possible.
- No runtime API traces yet.
- No full Dart analyzer integration yet; current parser is regex-based bootstrap.
- No RBAC/multi-branch support yet.
- If evidence is missing, the server should return `partial`/`not_found` instead of guessing.

## Recommended next tech-stack research

For accuracy + speed, evaluate:

1. **Dart Analysis Server / analyzer package** — authoritative AST, symbols, imports, type resolution.
2. **Semble** — fast local code/docs retrieval layer; useful as candidate search, not source of truth.
3. **SQLite FTS5** — deterministic exact keyword search for field/API/status names.
4. **Tree-sitter Dart** — fast fallback parser if full analyzer is heavy.
5. **OpenAPI/GraphQL/Postman parsers** — contract index for API field verification.
6. **Runtime trace ingestion** — optional debug logs that map screen/route to actual API calls and payload fields.
7. **OCR/vision outside MCP first** — AI client extracts screenshot text; MCP maps text to localization/widgets.
