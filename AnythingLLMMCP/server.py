#!/usr/bin/env python3
"""AnythingLLM MCP Server：只暴露一个工具，读取第一个工作区及其文件信息。

同时支持两种 MCP Streamable HTTP 形态：
  * 旧协议（2025-03-26 / 2025-06-18 / 2025-11-25）：initialize 握手，无会话、无 GET 长连接。
  * 新协议（2026-07-28 及以后）：无握手、无长连接，元数据随每个请求发送。
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.getenv("MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("MCP_PORT", "8082"))
MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN,) + LEGACY
SERVER_INFO = {"name": "anythingllm", "version": "1.0.0"}
CONFIG = Path(__file__).with_name("config.json")

TOOL = {
    "name": "get_first_workspace_files",
    "description": "读取本机 AnythingLLM 的第一个工作区，返回其信息和文件列表。",
    "inputSchema": {"type": "object", "properties": {}},
}


# ---------- AnythingLLM ----------
def config():
    data = {}
    if CONFIG.exists():
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    base = os.getenv("ANYTHINGLLM_BASE_URL") or data.get("baseUrl") or "http://127.0.0.1:3001"
    key = os.getenv("ANYTHINGLLM_API_KEY") or data.get("apiKey") or ""
    return base.rstrip("/"), key


def anythingllm_get(path):
    base, key = config()
    if not key:
        raise RuntimeError(
            "未配置 AnythingLLM API Key：请设置环境变量 ANYTHINGLLM_API_KEY，"
            "或在 config.json 中填写 apiKey（在 AnythingLLM 设置中创建）。"
        )
    req = urllib.request.Request(
        base + path,
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("AnythingLLM 请求失败（HTTP %s）：%s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise RuntimeError("无法连接 AnythingLLM（%s）：%s" % (base, exc.reason))


def first_workspace_files():
    listed = anythingllm_get("/api/v1/workspaces")
    workspaces = listed.get("workspaces") or []
    if not workspaces:
        raise RuntimeError("AnythingLLM 中没有任何工作区。")
    first = workspaces[0]
    slug = first.get("slug")
    if not slug:
        raise RuntimeError("第一个工作区缺少 slug 字段。")
    detail = anythingllm_get("/api/v1/workspace/" + urllib.parse.quote(str(slug), safe=""))
    workspace = detail.get("workspace") or first
    if isinstance(workspace, list):  # 兼容文档示例中的数组形态
        workspace = workspace[0] if workspace else first
    files = workspace.get("documents") or []
    return {
        "workspace": {
            "id": workspace.get("id"),
            "name": workspace.get("name"),
            "slug": workspace.get("slug") or slug,
        },
        "fileCount": len(files),
        "files": files,
    }


# ---------- JSON-RPC / MCP ----------
def server_meta():
    return {"io.modelcontextprotocol/serverInfo": SERVER_INFO}


def decode_header_value(value):
    if value and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except Exception:
            return value
    return value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AnythingLLMMCP/1.0"

    def log_message(self, fmt, *args):
        print("[anythingllm] " + (fmt % args))

    # ---- HTTP helpers ----
    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            host = urllib.parse.urlparse(origin).hostname
        except ValueError:
            return False
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, status, payload=None):
        if payload is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status, request_id, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send(status, {"jsonrpc": "2.0", "id": request_id, "error": err})

    def _version(self, params):
        header = (self.headers.get("MCP-Protocol-Version") or "").strip()
        meta = params.get("_meta") or {}
        body = meta.get("io.modelcontextprotocol/protocolVersion") or ""
        return header, body

    # ---- request handling ----
    def do_POST(self):
        if urllib.parse.urlparse(self.path).path not in ("/mcp", "/"):
            self._send(404, {"error": "not found"})
            return
        if not self._origin_ok():
            self._error(403, None, -32000, "Forbidden origin")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._error(400, None, -32700, "Parse error")
            return

        if isinstance(body, list):
            responses = []
            for item in body:
                if not isinstance(item, dict):
                    responses.append({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32600, "message": "Invalid Request"}})
                    continue
                if "id" not in item:
                    continue
                _, payload = self._dispatch(item)
                if payload is not None:
                    responses.append(payload)
            if responses:
                self._send(200, responses)
            else:
                self._send(202)
            return

        if not isinstance(body, dict):
            self._error(400, None, -32600, "Invalid Request")
            return
        if "id" not in body:  # 通知，一律接收
            self._send(202)
            return
        if body.get("id") is None:
            self._error(400, None, -32600, "Invalid Request: id must not be null")
            return

        status, payload = self._dispatch(body)
        if payload is None:
            self._send(status)
        else:
            self._send(status, payload)

    def _dispatch(self, msg):
        request_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(method, str):
            return 400, self._rpc_error(request_id, -32600, "Invalid Request")
        header_version, body_version = self._version(params)
        version = header_version or body_version
        modern = method == "server/discover" or (
            bool(version) and version >= MODERN
        )

        if modern:
            if not version:
                version = MODERN
            if header_version and body_version and header_version != body_version:
                return 400, self._rpc_error(
                    request_id, -32020,
                    "Header mismatch: MCP-Protocol-Version %r != body version %r"
                    % (header_version, body_version),
                )
            if version not in SUPPORTED:
                return 400, self._rpc_error(
                    request_id, -32022, "Unsupported protocol version",
                    {"supported": list(SUPPORTED), "requested": version},
                )
            if self.headers.get("Mcp-Method") != method:
                return 400, self._rpc_error(
                    request_id, -32020,
                    "Header mismatch: Mcp-Method is missing or does not match body method",
                )
            if method == "tools/call":
                name = decode_header_value(self.headers.get("Mcp-Name"))
                if name != params.get("name"):
                    return 400, self._rpc_error(
                        request_id, -32020,
                        "Header mismatch: Mcp-Name is missing or does not match params.name",
                    )
            return self._modern_call(request_id, method, params)

        if header_version and header_version not in SUPPORTED:
            return 400, self._rpc_error(
                request_id, -32600, "Unsupported protocol version: " + header_version
            )
        return self._legacy_call(request_id, method, params)

    def _rpc_error(self, request_id, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}

    def _modern_call(self, request_id, method, params):
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": list(SUPPORTED),
                "capabilities": {"tools": {}},
                "_meta": server_meta(),
                "instructions": TOOL["description"],
                "ttlMs": 3600000,
                "cacheScope": "public",
            }
            return 200, self._result(request_id, result)
        if method == "ping":
            return 200, self._result(request_id, {"resultType": "complete", "_meta": server_meta()})
        if method == "tools/list":
            return 200, self._result(
                request_id,
                {
                    "resultType": "complete",
                    "tools": [TOOL],
                    "_meta": server_meta(),
                    "ttlMs": 3600000,
                    "cacheScope": "public",
                },
            )
        if method == "tools/call":
            return 200, self._result(request_id, self._tool_result(params, modern=True))
        return 404, self._rpc_error(request_id, -32601, "Method not found: " + method)

    def _legacy_call(self, request_id, method, params):
        if method == "initialize":
            requested = params.get("protocolVersion")
            negotiated = requested if requested in SUPPORTED else LEGACY[0]
            result = {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": TOOL["description"],
            }
            return 200, self._result(request_id, result)
        if method == "ping":
            return 200, self._result(request_id, {})
        if method == "tools/list":
            return 200, self._result(request_id, {"tools": [TOOL]})
        if method == "tools/call":
            return 200, self._result(request_id, self._tool_result(params, modern=False))
        return 200, self._rpc_error(request_id, -32601, "Method not found: " + method)

    def _result(self, request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _tool_result(self, params, modern):
        if params.get("name") != TOOL["name"]:
            result = {
                "content": [{"type": "text", "text": "未知工具：" + str(params.get("name"))}],
                "isError": True,
            }
        else:
            try:
                data = first_workspace_files()
                result = {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                    "isError": False,
                }
            except Exception as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        if modern:
            result["resultType"] = "complete"
            result["_meta"] = server_meta()
        return result

    # 新协议不再提供 GET 长连接；旧协议中 GET 也是可选的。
    def do_GET(self):
        if urllib.parse.urlparse(self.path).path not in ("/mcp", "/"):
            self._send(404, {"error": "not found"})
            return
        self._send(405, {"error": "Use POST /mcp"})

    def do_DELETE(self):
        self._send(405, {"error": "No sessions"})


def main():
    print("AnythingLLM MCP: http://%s:%d/mcp" % (HOST, PORT))
    print("Protocols: %s" % ", ".join(SUPPORTED))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
