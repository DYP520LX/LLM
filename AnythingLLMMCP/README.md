# AnythingLLM MCP Server

一个只做一件事的 MCP Server：读取本机 AnythingLLM 的**第一个工作区**及其文件信息。

- 传输：Streamable HTTP，端点 `http://127.0.0.1:8082/mcp`
- 协议：同时支持
  - 旧版 `2025-03-26` / `2025-06-18` / `2025-11-25`（`initialize` 握手）
  - 新版 `2026-07-28` 及以后（无握手、无长连接，元数据随每个请求发送）
- 工具：`get_first_workspace_files`，无参数，返回工作区信息与文件列表（`files` 为 AnythingLLM 原始文档信息）

## 配置

1. 在 AnythingLLM 的「设置 → 开发者 API」中创建一个 API Key。
2. 填入 `config.json`：

```json
{
  "baseUrl": "http://127.0.0.1:3001",
  "apiKey": "你的-API-Key"
}
```

也可以用环境变量 `ANYTHINGLLM_API_KEY`、`ANYTHINGLLM_BASE_URL` 覆盖。

## 运行

```powershell
cd D:\学习\wd\LLM\AnythingLLMMCP
python server.py
```

只使用 Python 标准库，无需安装依赖。MCP 客户端配置见
`D:\学习\wd\LLM\Playground\mcp.json`。
