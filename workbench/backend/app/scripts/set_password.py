"""管理员命令行：设置/重置用户密码，或创建新用户。

用法（在 workbench/backend 目录下执行）：
    python -m app.scripts.set_password <user_id> <password> [显示名]
    python -m app.scripts.set_password --list

说明：
- 密码只写入 PBKDF2-SHA256 强哈希，不落明文；
- 重置密码后该用户的所有既有会话立即失效。
"""
import sys

from .. import db
from ..services import auth


def main(argv: list[str]) -> int:
    db.init_db()
    if "--list" in argv:
        for u in db.query("SELECT id,name,role,status,organization_id,last_login_at FROM users ORDER BY created_at ASC"):
            pwd = "已设置" if auth.user_has_password(u["id"]) else "未设置"
            print(f'{u["id"]:<28} {u["name"] or "":<16} {u["role"]:<18} {u["status"]:<10} 密码：{pwd}')
        return 0
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    user_id, password = args[0], args[1]
    name = args[2] if len(args) > 2 else None
    if not db.query_one("SELECT id FROM users WHERE id=?", (user_id,)):
        if not name:
            print(f"用户 {user_id} 不存在；新建用户时必须给显示名")
            return 2
        db.execute("INSERT INTO users(id,tenant_id,role,name,email,status,organization_id,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (user_id, "tnt_default", "editor", name, "", "active", "org_default", int(db.now())))
        db.execute("INSERT INTO memberships(id,user_id,organization_id,role,status,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (db.gen_id("mem"), user_id, "org_default", "editor", "active", int(db.now()), int(db.now())))
        print(f"已创建用户 {user_id}（角色 editor，组织 org_default）")
    if name:
        db.execute("UPDATE users SET name=? WHERE id=?", (name, user_id))
    try:
        auth.set_password(user_id, password)
    except ValueError as e:
        print(f"失败：{e}")
        return 2
    n = len(db.query("SELECT id FROM sessions WHERE user_id=? AND revoked_at IS NULL", (user_id,)))
    auth.revoke_all_sessions(user_id)
    db.audit(None, "cli", "auth.password.set", user_id)
    print(f"已更新 {user_id} 的密码，并撤销 {n} 个既有会话")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
