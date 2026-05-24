# Tích hợp app-context-mcp với Arkon

## Cách 1: External MCP Server (Client-side Config) ✅

Arkon và app-context-mcp là 2 MCP server độc lập. User đăng ký cả 2 trong AI client.

### Bước 1: Chạy app-context-mcp trong mạng nội bộ

```bash
cd app-context-mcp

# Tạo cert (chỉ làm 1 lần)
brew install mkcert nss
mkcert -install
mkcert <ip-mac-của-bạn> localhost

# Chạy server
uv run app-context-mcp \
  --transport sse \
  --https \
  --host 0.0.0.0 \
  --port 8443 \
  --ssl-certfile ./<ip>+1.pem \
  --ssl-keyfile ./<ip>+1-key.pem
```

### Bước 2: Lấy IP máy Mac

```bash
ipconfig getifaddr en0
```

Giả sử IP là `192.168.1.25`.

### Bước 3: Config Claude Desktop / Cursor / Windsurf

File: `~/Library/Application\ Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "arkon": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <your-arkon-mcp-token>"
      }
    },
    "app-context": {
      "url": "https://192.168.1.25:8443/sse"
    }
  }
}
```

### Bước 4: Cách dùng

**Khi cần kiến thức doanh nghiệp / wiki:**
```
Dùng tool từ arkon: search_wiki, read_wiki_page...
```

**Khi cần trace Flutter codebase:**
```
Dùng tool từ app-context: ask_app_context_tool
```

**Khi cần cả 2 (phối hợp):**
```
1. Hỏi Arkon: "Tính năng Hủy đơn có trong design doc không?"
2. Hỏi app-context: "Code hiện tại implement field canCancel thế nào?"
3. So sánh: design doc nói A, code làm B → tìm gap
```

---

## Cách 2: Arkon Resource Type (Yêu cầu extend Arkon)

Đăng ký `flutter_repo` là resource type trong Arkon.

### Schema mẫu trong Arkon DB:

```sql
-- Thêm bảng flutter_repo contexts
CREATE TABLE flutter_repo_contexts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,           -- e.g. "order-app"
    repo_path TEXT NOT NULL,      -- local path
    sse_url TEXT,                 -- https://192.168.1.25:8443/sse
    semble_index_path TEXT,       -- path to Semble index
    code_graph_json JSONB,        -- CodeGraph export
    git_commit TEXT,
    git_branch TEXT,
    last_synced TIMESTAMP,
    knowledge_type_slugs TEXT[]   -- ["engineering", "flutter"]
);
```

### Flow:
```
Admin register repo → Arkon DB
AI client query: "Hủy đơn trong order-app"
Arkon tool: lookup repo → forward to app-context-mcp SSE
→ Trả kết quả
```

### Yêu cầu:
- Thêm `app/mcp/tools_flutter_repo.py` vào Arkon source
- Hoặc PR lên Arkon repo

---

## Cách 3: Two-way Sync (Wiki Push)

App-context-mcp periodically push evidence lên Arkon wiki.

### Docker cron hoặc systemd service:

```bash
# Mỗi 5 phút, scan repo changes
# Nếu có commit mới → push CodeGraph + Semble index lên Arkon
```

### Script mẫu:

```python
# push_to_arkon.py
from app_context_mcp.retrieval import SembleRetrieval
from app_context_mcp.code_graph import build_code_graph
from app_context_mcp.indexer import build_index

def sync_to_arkon(repo_path: str, arkon_api_base: str, token: str):
    index = build_index(repo_path)
    cg = build_code_graph(index)

    # Push CodeGraph as wiki page
    export = cg.export()
    post_to_arkon(
        arkon_api_base + "/wiki/pages",
        token,
        {
            "slug": f"flutter-graph-{Path(repo_path).name}",
            "title": f"CodeGraph: {Path(repo_path).name}",
            "content_md": json_to_markdown_table(export),
        }
    )
```

### Lợi ích:
- Team không cần connect MCP server
- Đọc wiki trên Arkon web UI
- AI client query qua Arkon MCP (1 endpoint duy nhất)

### Tradeoff:
- Latency: wiki có thể stale
- Cần sync mechanism

---

## So sánh nhanh

| Tiêu chí | Cách 1 | Cách 2 | Cách 3 |
|---|---|---|---|
| Độ phức tạp | Thấp | Cao | Trung bình |
| Cần sửa Arkon | Không | Có | Không (chỉ Arkon API) |
| Latency | Thấp | Thấp | Cao (sync interval) |
| Stale data | Không | Không | Có |
| AI client config | 2 MCP servers | 1 MCP server | 1 MCP server |
| Khuyến nghị | **Dùng ngay** | PR Arkon | Long-term |
