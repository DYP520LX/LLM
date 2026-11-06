# 宠物医院 MCP（MVP）：只暴露 list_pets。
# Streamable HTTP 单端点，兼容旧版 initialize 握手与 2026-07-28 无握手/无会话/无 GET 长连接。
import base64, json, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8081
API = "http://127.0.0.1:8080/api/v1/pets"
NAME, VERSION = "pet-hospital", "0.1.0"
MODERN = "2026-07-28"
VERSIONS = [MODERN, "2025-11-25", "2025-06-18", "2025-03-26"]
LEGACY = VERSIONS[1:]
INFO = {"name": NAME, "version": VERSION}
CAPS = {"tools": {}}
FIELDS = ("q", "name", "ownerName", "ownerPhone", "species", "doctor", "disease",
          "status", "min", "max", "sortBy", "order", "page", "pageSize")
SORT_FIELDS = ["id", "name", "ownerName", "species", "doctor", "disease", "status",
               "totalCost", "visitCount", "createdAt", "updatedAt"]
SPECIES = ["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
STATUSES = ["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
TOOL = {
    "name": "list_pets",
    "description": "查询宠物医院档案列表（GET /api/v1/pets）。支持关键字与字段过滤、总花费区间、排序和分页；返回 items 及 total/page/totalPages/totalCost。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "关键字"},
            "name": {"type": "string", "description": "宠物姓名"},
            "ownerName": {"type": "string", "description": "主人姓名"},
            "ownerPhone": {"type": "string", "description": "主人电话"},
            "species": {"type": "string", "enum": SPECIES},
            "doctor": {"type": "string", "description": "主治医生"},
            "disease": {"type": "string", "description": "疾病/诊断"},
            "status": {"type": "string", "enum": STATUSES},
            "min": {"type": "number", "minimum": 0, "description": "总花费下限"},
            "max": {"type": "number", "minimum": 0, "description": "总花费上限"},
            "sortBy": {"type": "string", "enum": SORT_FIELDS},
            "order": {"type": "string", "enum": ["asc", "desc"]},
            "page": {"type": "integer", "minimum": 1, "default": 1},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "additionalProperties": False,
    },
}


def list_pets(args):
    if not isinstance(args, dict):
        raise ValueError("arguments 必须是对象")
    unknown = set(args) - set(FIELDS)
    if unknown:
        raise ValueError("未知参数: " + ", ".join(sorted(unknown)))
    query = {}
    for key, val in args.items():
        if val is None or val == "":
            continue
        if key in ("min", "max"):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
                raise ValueError(key + " 必须是非负数")
        elif key in ("page", "pageSize"):
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(key + " 必须是整数")
            if val < 1 or (key == "pageSize" and val > 100):
                raise ValueError(key + " 超出范围")
        elif not isinstance(val, str):
            raise ValueError(key + " 必须是字符串")
        elif key == "species" and val not in SPECIES:
            raise ValueError("species 不受支持")
        elif key == "status" and val not in STATUSES:
            raise ValueError("status 不受支持")
        query[key] = val
    if query.get("sortBy") not in (None, *SORT_FIELDS):
        raise ValueError("sortBy 不受支持")
    if query.get("order") not in (None, "asc", "desc"):
        raise ValueError("order 必须是 asc 或 desc")
    url = API + ("?" + urllib.parse.urlencode(query) if query else "")
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError("上游 API 返回 %s: %s" % (exc.code, detail)) from exc
    except Exception as exc:
        raise RuntimeError("调用上游 API 失败: %s" % exc) from exc


def call_tool(name, args):
    if name != "list_pets":
        raise KeyError(name)
    return list_pets(args or {})


def decode_header(value):
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except Exception:
            return None
    return value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[pet-hospital-mcp]", fmt % args)

    def _header(self, name):
        return (self.headers.get(name) or "").strip()

    def _origin_bad(self):
        origin = self._header("Origin")
        if not origin:
            return False
        return urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1")

    def _send(self, status, payload=None, headers=None, sse=False):
        if payload is None:
            self.send_response(status)
            for key, val in (headers or {}).items():
                self.send_header(key, val)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        if sse:
            data = b"event: message\ndata: " + raw + b"\n\n"
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def _rpc(self, rid, result=None, error=None, status=200, modern=False, sse=False):
        payload = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            payload["error"] = error
        else:
            result = result if result is not None else {}
            if modern:
                result["resultType"] = "complete"
                meta = dict(result.get("_meta") or {})
                meta["io.modelcontextprotocol/serverInfo"] = INFO
                result["_meta"] = meta
            payload["result"] = result
        self._send(status, payload, sse=sse)

    def _error(self, rid, code, message, status=200, modern=False, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._rpc(rid, error=error, status=status, modern=modern)

    def _modern(self, body):
        header = self._header("MCP-Protocol-Version")
        meta = ((body or {}).get("params") or {}).get("_meta") or {}
        version = ""
        if isinstance(meta, dict):
            version = meta.get("io.modelcontextprotocol/protocolVersion") or ""
        return bool(header and header >= MODERN) or bool(version and version >= MODERN)

    def _validate_modern(self, body):
        """返回 (code, message, status, data)，None 表示通过。"""
        header = self._header("MCP-Protocol-Version")
        meta = (body.get("params") or {}).get("_meta") or {}
        if not header:
            return -32020, "缺少 MCP-Protocol-Version 请求头", 400, None
        if not isinstance(meta, dict) or not meta.get("io.modelcontextprotocol/protocolVersion"):
            return -32602, "缺少 _meta.io.modelcontextprotocol/protocolVersion", 400, None
        if header != meta["io.modelcontextprotocol/protocolVersion"]:
            return -32020, "MCP-Protocol-Version 与请求体不一致", 400, None
        if header not in VERSIONS or header < MODERN:
            return -32022, "不支持的协议版本", 400, {
                "supported": [MODERN], "requested": header}
        if not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
            return -32602, "缺少 _meta.io.modelcontextprotocol/clientCapabilities", 400, None
        if self._header("Mcp-Method") != body.get("method"):
            return -32020, "Mcp-Method 与请求体不一致或缺失", 400, None
        if body.get("method") == "tools/call":
            name = (body.get("params") or {}).get("name") or ""
            if decode_header(self._header("Mcp-Name")) != name:
                return -32020, "Mcp-Name 与 params.name 不一致或缺失", 400, None
        return None

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, {"name": NAME, "version": VERSION,
                             "mcp": "/mcp", "api": API})
        elif path == "/mcp":
            self._send(405, {"error": "无长连接，请使用 POST"}, {"Allow": "POST"})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        self._send(405, {"error": "无会话，请使用 POST"}, {"Allow": "POST"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path not in ("/mcp", "/"):
            self._send(404, {"error": "not found"})
            return
        if self._origin_bad():
            self._send(403, {"error": "invalid origin"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._error(None, -32700, "Parse error", status=400)
            return
        if not isinstance(body, dict) or not isinstance(body.get("method"), str):
            self._error(body.get("id") if isinstance(body, dict) else None,
                        -32600, "Invalid Request", status=400)
            return

        method, rid = body["method"], body.get("id")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            self._error(rid, -32602, "params 必须是对象", status=400)
            return
        sse = "text/event-stream" in self._header("Accept") and \
              "application/json" not in self._header("Accept")
        modern = self._modern(body)

        if method.startswith("notifications/"):
            self._send(202)
            return
        if rid is None:
            self._error(None, -32600, "Invalid Request", status=400, modern=modern)
            return
        if modern:
            failure = self._validate_modern(body)
            if failure:
                self._error(rid, *failure[:2], status=failure[2], modern=True, data=failure[3])
                return
            result = self._modern_method(rid, method, params, sse)
            if result is None:
                self._error(rid, -32601, "Method not found", status=404, modern=True)
            return

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in LEGACY else "2025-11-25"
            self._rpc(rid, {"protocolVersion": version, "capabilities": CAPS,
                            "serverInfo": INFO,
                            "instructions": "宠物医院档案查询；请调用 list_pets。"},
                      sse=sse)
        elif method == "ping":
            self._rpc(rid, {}, sse=sse)
        elif method == "tools/list":
            self._rpc(rid, {"tools": [TOOL]}, sse=sse)
        elif method == "tools/call":
            self._tool_call(rid, params, sse, modern=False)
        else:
            self._error(rid, -32601, "Method not found", status=200)

    def _modern_method(self, rid, method, params, sse):
        if method == "server/discover":
            self._rpc(rid, {"supportedVersions": VERSIONS, "capabilities": CAPS,
                            "instructions": "宠物医院档案查询；请调用 list_pets。",
                            "ttlMs": 3600000, "cacheScope": "public"},
                      modern=True, sse=sse)
            return True
        if method == "tools/list":
            self._rpc(rid, {"tools": [TOOL], "ttlMs": 3600000,
                            "cacheScope": "public"}, modern=True, sse=sse)
            return True
        if method == "tools/call":
            self._tool_call(rid, params, sse, modern=True)
            return True
        return None

    def _tool_call(self, rid, params, sse, modern):
        try:
            data = call_tool(params.get("name"), params.get("arguments") or {})
            result = {"content": [{"type": "text",
                                   "text": json.dumps(data, ensure_ascii=False)}],
                      "isError": False}
        except KeyError:
            self._error(rid, -32602, "Unknown tool: %s" % params.get("name"),
                        status=400 if modern else 200, modern=modern)
            return
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        self._rpc(rid, result, modern=modern, sse=sse)


if __name__ == "__main__":
    print("pet-hospital MCP: http://%s:%s/mcp  ->  %s" % (HOST, PORT, API))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
