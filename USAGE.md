# Cách sử dụng App Context MCP

MCP này expose **1 tool duy nhất**: `ask_app_context_tool`.

Mục tiêu: AI client connect vào MCP, không cần đọc cả Flutter codebase. MCP tự index/trace codebase và trả context/evidence pack để AI client suy luận.

## 1. Yêu cầu trên macOS

Cài `uv`:

```bash
brew install uv
```

Nếu muốn HTTPS cert tin cậy trong LAN, cài `mkcert`:

```bash
brew install mkcert nss
mkcert -install
```

## 2. Giải nén và chạy test

```bash
unzip app-context-mcp.zip
cd app-context-mcp
uv run --extra dev pytest -q
```

Kỳ vọng:

```txt
4 passed
```

## 3. Chạy thử bằng CLI

```bash
uv run app-context-mcp \
  --repo tests/fixtures/flutter_app \
  --question "Màn chi tiết đơn hàng vì sao không hiện nút Hủy đơn? API field nào quyết định?" \
  --screenshot-text "Chi tiết đơn hàng Hủy đơn Đang giao"
```

## 4. Chạy HTTPS + SSE cho AI client trong mạng nội bộ

Lấy IP Mac:

```bash
ipconfig getifaddr en0
```

Ví dụ IP là `192.168.1.25`.

Tạo cert bằng `mkcert`:

```bash
mkcert 192.168.1.25 localhost
```

Nó sẽ tạo file kiểu:

```txt
192.168.1.25+1.pem
192.168.1.25+1-key.pem
```

Chạy server:

```bash
uv run app-context-mcp \
  --transport sse \
  --https \
  --host 0.0.0.0 \
  --port 8443 \
  --ssl-certfile ./192.168.1.25+1.pem \
  --ssl-keyfile ./192.168.1.25+1-key.pem
```

AI client connect URL:

```txt
https://192.168.1.25:8443/sse
```

## 5. Config AI client dạng SSE

Nếu AI client hỗ trợ MCP SSE URL:

```json
{
  "mcpServers": {
    "app-context": {
      "url": "https://192.168.1.25:8443/sse"
    }
  }
}
```

Thay `192.168.1.25` bằng IP Mac của bạn.

## 6. Nếu muốn chạy nhanh bằng self-signed cert

```bash
uv run app-context-mcp \
  --transport sse \
  --https \
  --generate-self-signed \
  --host 0.0.0.0 \
  --port 8443
```

Lưu ý: nhiều AI client sẽ reject self-signed cert nếu cert chưa được trust.

## 7. Input tool nên gửi

AI client nên gửi càng nhiều context càng tốt:

```json
{
  "repo_path": "/path/to/flutter_app",
  "question": "Nếu muốn hiện lý do vì sao không hủy được đơn thì API cần workaround như nào?",
  "screenshot_text": "Chi tiết đơn hàng Hủy đơn Đang giao",
  "user_role": "NON_TECH",
  "desired_behavior": "Show reason why order cannot be cancelled",
  "max_results": 20
}
```

## 8. Output MCP trả

MCP trả evidence pack:

```txt
screen_candidates
case_flow_candidates
code_trace_graph
ui_elements
api_usage
api_field_lineage
current_logic_facts
desired_behavior_analysis
five_w_one_h
socratic_followups
evidence_registry
unknowns_and_limits
index_info
```

AI client dùng output đó để tự suy luận và trả lời người dùng.

## 9. Giới hạn bản MVP

- Parser hiện là regex-based bootstrap, chưa phải Dart analyzer đầy đủ.
- Chưa có Semble/SQLite FTS5/runtime trace.
- Chưa OCR ảnh trực tiếp; nên để AI client extract `screenshot_text`.
- Không có LLM trong MCP server.
- Nếu thiếu evidence, server trả partial/not_found, không đoán.

## 10. Bước nâng cấp tiếp theo

Ưu tiên:

```txt
Dart analyzer
Semble retrieval
SQLite FTS5
API contract parser
runtime trace ingestion
well-known discovery endpoint
auto client config installer
```
