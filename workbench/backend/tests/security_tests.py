"""第 1 批安全验收测试：真实身份入口（认证必须来自服务端会话）。

无第三方依赖，直接用标准库起一个隔离的服务实例（独立临时数据库 + 独立端口），
跑完断言后自行清理，不触碰开发数据库。

运行（在 workbench/backend 目录下）：
    python tests/security_tests.py

退出码 0 表示全部通过；任何一条失败即非 0，失败详情会打印在末尾。
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

ADMIN_PW = "AdminPass123"
EDITOR_PW = "EditorPass123"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    """带 Cookie 的 HTTP 客户端（模拟浏览器会话）。"""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method: str, path: str, body=None, headers=None):
        # 默认带 Origin：模拟浏览器同源请求，满足第 2 批的 CSRF 同源校验
        h = {"Content-Type": "application/json", "Origin": self.base}
        if headers:
            for k, v in headers.items():
                if v is None:
                    h.pop(k, None)
                else:
                    h[k] = v
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(r, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    def clear_cookies(self):
        self.jar.clear()


def wait_health(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="wb_sec_")
    db_path = os.path.join(tmpdir, "test.db")
    data_dir = os.path.join(tmpdir, "data")
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env["WB_DB_PATH"] = db_path
    env["WB_UPLOAD_DIR"] = data_dir
    env["WB_ENV"] = "test"
    env.pop("WB_DEV_AUTH", None)          # 关键：默认关闭开发放行
    env.pop("WB_API_TOKEN", None)
    env.pop("WB_ADMIN_TOKEN", None)

    print(f"[setup] 隔离实例：db={db_path} port={port}")
    proc = subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not wait_health(base):
            out = b""
            try:
                proc.terminate()
                out = proc.stdout.read() if proc.stdout else b""
            except Exception:
                pass
            print("服务未能启动：\n" + out.decode("utf-8", "replace")[-3000:])
            return 1

        anon = Client(base)
        admin = Client(base)
        editor = Client(base)

        print("\n[未登录访问必须被拒绝]")
        st, _ = anon.req("GET", "/api/projects")
        check("未登录读项目列表 → 401", st == 401, f"got {st}")

        st, _ = anon.req("GET", "/api/projects", headers={"X-WB-Role": "super_admin"})
        check("未登录 + X-WB-Role: super_admin 读项目列表 → 401", st == 401, f"got {st}")

        st, _ = anon.req("GET", "/api/admin/users")
        check("未登录读后台成员列表 → 401", st == 401, f"got {st}")

        st, _ = anon.req("GET", "/api/admin/cost-summary")
        check("未登录读成本汇总 → 401", st == 401, f"got {st}")

        st, _ = anon.req("GET", "/files/ast_whatever")
        check("未登录取资产字节 /files/<id> → 401", st == 401, f"got {st}")

        st, _ = anon.req("PUT", "/api/admin/organizations/org_default/quota", {"quota_total": 1},
                         headers={"X-WB-Role": "super_admin"})
        check("未登录 + 伪造超管头改组织额度 → 401", st == 401, f"got {st}")

        print("\n[首次初始化]")
        st, body = anon.req("GET", "/api/auth/bootstrap-state")
        check("初始化状态接口可匿名访问", st == 200, f"got {st}")
        check("开发放行模式默认关闭（dev_mode=false）", isinstance(body, dict) and body.get("dev_mode") is False, str(body))
        check("初始状态需要初始化管理员密码", isinstance(body, dict) and body.get("needs_bootstrap") is True, str(body))

        st, _ = anon.req("POST", "/api/auth/login", {"user_id": "usr_default", "password": ADMIN_PW})
        check("未初始化密码时登录失败 → 401", st == 401, f"got {st}")

        st, body = admin.req("POST", "/api/auth/bootstrap", {"user_id": "usr_default", "password": ADMIN_PW, "name": "管理员"})
        check("初始化管理员密码 → 201", st == 201, f"got {st} {body}")

        print("\n[管理员身份由服务端确认]")
        st, me = admin.req("GET", "/api/auth/me")
        check("管理员 /api/auth/me → 200", st == 200, f"got {st}")
        check("角色来自服务端（super_admin）", isinstance(me, dict) and me.get("role") == "super_admin", str(me)[:160])
        check("权限列表由服务端下发且包含成员管理",
              isinstance(me, dict) and "member.manage" in (me.get("permissions") or []), str(me)[:160])

        st, _ = admin.req("GET", "/api/admin/users")
        check("管理员可读后台成员列表 → 200", st == 200, f"got {st}")

        st, org = admin.req("GET", "/api/admin/organizations/org_default/quota")
        check("管理员可读组织额度 → 200", st == 200, f"got {st}")

        print("\n[编辑角色：伪造管理员头无效]")
        st, created = admin.req("POST", "/api/admin/organizations/org_default/members",
                                {"name": "编辑小李", "email": "li@example.com", "role": "editor"})
        check("管理员邀请编辑成员 → 201", st == 201, f"got {st} {created}")
        editor_uid = (created or {}).get("user_id") if isinstance(created, dict) else None
        check("邀请返回 user_id", bool(editor_uid), str(created))

        st, _ = admin.req("POST", f"/api/admin/users/{editor_uid}/reset-password", {"new_password": EDITOR_PW})
        check("管理员为成员设置初始密码 → 200", st == 200, f"got {st}")

        st, body = editor.req("POST", "/api/auth/login", {"user_id": editor_uid, "password": EDITOR_PW})
        check("编辑登录 → 200", st == 200, f"got {st} {body}")

        st, me = editor.req("GET", "/api/auth/me")
        check("编辑 /api/auth/me 角色为 editor", isinstance(me, dict) and me.get("role") == "editor", str(me)[:160])

        st, _ = editor.req("PUT", "/api/admin/organizations/org_default/quota", {"quota_total": 999},
                           headers={"X-WB-Role": "super_admin"})
        check("编辑 + 伪造超管头改额度 → 403", st == 403, f"got {st}")

        st, _ = editor.req("GET", "/api/admin/users", headers={"X-WB-Role": "super_admin"})
        check("编辑 + 伪造超管头读成员列表 → 403", st == 403, f"got {st}")

        st, _ = editor.req("POST", "/api/admin/reviews/rev_none/approve", {}, headers={"X-WB-Role": "super_admin"})
        check("编辑 + 伪造超管头做审核裁决 → 403", st == 403, f"got {st}")

        st, proj = editor.req("POST", "/api/projects", {"name": "编辑的项目"})
        check("编辑可以创建自己的项目", st in (200, 201), f"got {st}")
        check("新建项目返回有效 id", isinstance(proj, dict) and bool(proj.get("id")), str(proj)[:120])

        print("\n[会话可撤销]")
        st, _ = editor.req("POST", "/api/auth/logout")
        check("退出登录 → 200", st == 200, f"got {st}")
        st, _ = editor.req("GET", "/api/auth/me")
        check("退出后原会话不可用 → 401", st == 401, f"got {st}")

        st, _ = editor.req("POST", "/api/auth/login", {"user_id": editor_uid, "password": EDITOR_PW})
        check("重新登录 → 200", st == 200, f"got {st}")
        st, _ = admin.req("POST", f"/api/admin/users/{editor_uid}/disable", {})
        check("管理员禁用成员 → 200", st == 200, f"got {st}")
        st, _ = editor.req("GET", "/api/auth/me")
        check("禁用后原会话立即失效 → 401", st == 401, f"got {st}")
        st, _ = editor.req("POST", "/api/auth/login", {"user_id": editor_uid, "password": EDITOR_PW})
        check("禁用后无法重新登录 → 401", st == 401, f"got {st}")

        print("\n[密码存储与二次初始化]")
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT password_hash FROM users WHERE id='usr_default'").fetchone()
        conn.close()
        ph = (row or [""])[0] or ""
        check("密码以强哈希保存（pbkdf2_sha256）", ph.startswith("pbkdf2_sha256$"), ph[:24])
        check("数据库中不含明文密码", ADMIN_PW not in ph, "明文泄露")
        st, _ = anon.req("POST", "/api/auth/bootstrap", {"user_id": "usr_other", "password": "Whatever123"})
        check("已初始化后再次 bootstrap → 409", st == 409, f"got {st}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 68)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print("第 1 批安全验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
