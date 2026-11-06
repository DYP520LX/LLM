# 上传文档到 AnythingLLM 的第一个工作区并完成向量化嵌入
# 运行: python server.py   打开 http://127.0.0.1:8083/
import json, mimetypes, re, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 8083
BASE = "http://127.0.0.1:3001"  # AnythingLLM 服务地址
API_KEY = "30DRV5P-2V34VM0-JJYPFRS-6AYFT1M"
PAGE = (Path(__file__).parent / "index.html").read_bytes()


def api(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="GET" if payload is None else "POST")
    req.add_header("Authorization", "Bearer " + API_KEY)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload(filename, content, slug):
    """POST /api/v1/document/upload，addToWorkspaces 会让服务端直接完成嵌入。"""
    boundary = "----wd" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"addToWorkspaces\"\r\n\r\n{slug}\r\n".encode("utf-8")
        + f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n".encode("utf-8")
        + f"Content-Type: {ctype}\r\n\r\n".encode("utf-8")
        + content
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    req = urllib.request.Request(BASE + "/api/v1/document/upload", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + API_KEY)
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_form(content_type, body):
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        raise ValueError("不是 multipart 表单")
    boundary = (m.group(1) or m.group(2)).encode()
    for chunk in body.split(b"--" + boundary):
        if b"Content-Disposition" not in chunk:
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        data = data[:-2] if data.endswith(b"\r\n") else data
        if 'name="file"' in head.decode("utf-8", "replace"):
            fn = re.search(r'filename="([^"]*)"', head.decode("utf-8", "replace"))
            return fn.group(1) if fn else "", data
    return "", b""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[webpage] " + (fmt % args))

    def _send(self, code, raw, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path != "/":
            self.send_error(404)
            return
        self._send(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/upload":
            self.send_error(404)
            return
        try:
            form = parse_form(self.headers.get("Content-Type"),
                              self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            filename, content = form
            if not content:
                raise RuntimeError("请选择文件")

            workspaces = api("/api/v1/workspaces").get("workspaces") or []
            if not workspaces:
                raise RuntimeError("没有工作区，请先在 AnythingLLM 中创建")
            workspace = workspaces[0]

            result = upload(filename or "upload.txt", content, workspace["slug"])
            docs = result.get("documents") or []
            if not docs or not docs[0].get("location"):
                raise RuntimeError("上传失败：" + json.dumps(result, ensure_ascii=False))
            self._json(200, {"ok": True, "workspace": workspace["name"],
                             "file": docs[0].get("title") or filename})
        except Exception as exc:
            detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else str(exc)
            self._json(400, {"ok": False, "error": detail})


if __name__ == "__main__":
    print("upload page http://%s:%s/" % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
