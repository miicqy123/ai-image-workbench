"""测试公共设施：隔离实例 + 带 Cookie 的 HTTP 客户端 + 断言收集。

无第三方依赖。测试进程与开发数据库完全隔离（独立临时库、独立端口、独立上传目录）。
"""
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
PY = sys.executable

def _make_png(w: int = 8, h: int = 8) -> bytes:
    """生成一个合法的最小 PNG（不依赖 Pillow）。"""
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = struct.pack(">I", len(data)) + tag + data
        return body + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


TINY_PNG = _make_png()


class Results:
    def __init__(self) -> None:
        self.items = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((name, bool(ok), detail))
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))

    def summary(self, title: str) -> int:
        failed = [r for r in self.items if not r[1]]
        print("\n" + "=" * 68)
        print(f"合计 {len(self.items)} 项，通过 {len(self.items) - len(failed)} 项，失败 {len(failed)} 项")
        if failed:
            print("失败项：")
            for name, _, detail in failed:
                print(f"  - {name}: {detail}")
            return 1
        print(title + "：全部通过")
        return 0


def _parse(resp):
    """按 Content-Type 解析响应：JSON → dict，文本 → str，二进制（如图片）→ bytes。"""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    data = resp.read()
    if "json" in ctype:
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return data.decode("utf-8", "replace")
    if ctype.startswith("text/") or not ctype:
        return data.decode("utf-8", "replace")
    return data


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    """带 Cookie 的 HTTP 客户端；默认发送同源 Origin（模拟浏览器）。"""

    def __init__(self, base: str, origin: str | None = None):
        self.base = base
        self.origin = origin if origin is not None else base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method: str, path: str, body=None, headers=None, raw: bytes = None,
            content_type: str = "application/json"):
        h = {"Content-Type": content_type}
        if self.origin:
            h["Origin"] = self.origin
        if headers:
            for k, v in headers.items():
                if v is None:
                    h.pop(k, None)
                else:
                    h[k] = v
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        else:
            data = None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(r, timeout=30) as resp:
                return resp.status, _parse(resp)
        except urllib.error.HTTPError as e:
            return e.code, _parse(e)

    def upload(self, path: str, filename: str, data: bytes, fields: dict | None = None):
        boundary = "----wbharnessboundary"
        parts = []
        for k, v in (fields or {}).items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                     f"Content-Type: image/png\r\n\r\n".encode())
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        return self.req("POST", path, raw=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}")

    def clear_cookies(self):
        self.jar.clear()


class Instance:
    """启动一个隔离的后端实例，用完自动清理。"""

    def __init__(self, extra_env: dict | None = None):
        self.tmpdir = tempfile.mkdtemp(prefix="wb_org_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ)
        self.env["WB_DB_PATH"] = self.db_path
        self.env["WB_UPLOAD_DIR"] = os.path.join(self.tmpdir, "uploads")
        self.env["WB_ENV"] = "test"
        self.env.pop("WB_DEV_AUTH", None)
        self.env.pop("WB_API_TOKEN", None)
        self.env.pop("WB_ADMIN_TOKEN", None)
        self.env.pop("WB_CSRF_ALLOW_NO_ORIGIN", None)
        if extra_env:
            self.env.update(extra_env)
        self.proc = None
        self.log_path = os.path.join(self.tmpdir, "server.log")
        self.log_file = None

    def __enter__(self):
        print(f"[setup] 隔离实例 db={self.db_path} port={self.port}")
        self.log_file = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=BACKEND, env=self.env, stdout=self.log_file, stderr=subprocess.STDOUT)
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=3) as r:
                    if r.status == 200:
                        return self
            except Exception:
                time.sleep(0.4)
        try:
            self.proc.terminate()
            out = self.proc.stdout.read() if self.proc.stdout else b""
        except Exception:
            out = b""
        raise RuntimeError("服务未能启动：\n" + out.decode("utf-8", "replace")[-3000:])

    def dump_log(self, lines: int = 60) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except Exception:
            return "(无日志)"

    def __exit__(self, exc_type, exc, tb):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
        if exc_type is not None:
            print("\n[服务端日志尾部]\n" + self.dump_log())
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        return False


def sql(db_path: str, statements, fetch=False):
    """直接对隔离库做测试数据准备（fixture）；不触碰开发库。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = []
    try:
        for stmt, params in statements:
            cur = conn.execute(stmt, params or ())
            if fetch:
                out.append([dict(r) for r in cur.fetchall()])
        conn.commit()
    finally:
        conn.close()
    return out
