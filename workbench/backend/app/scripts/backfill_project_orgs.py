"""历史项目归属回填（可重复运行）。

规则（对应第 2 批图纸）：
- 只处理 organization_id 为空的项目；
- 只有归属能**唯一确定**时才自动回填：owner 只属于一个组织，或 owner 有唯一 organization_id；
- owner 不存在、属于多个组织、或组织本身不存在 → 列入待人工处理清单，不猜；
- 归属不明的项目对组织级角色不可见（由 authz 保证），平台管理员可在后台指定归属；
- 运行前自动备份数据库文件，并写迁移结果记录（migrations 表）与 JSON 报告。

用法（在 workbench/backend 目录下执行）：
    python -m app.scripts.backfill_project_orgs            # 执行
    python -m app.scripts.backfill_project_orgs --dry-run   # 只看结果不写入
"""
import json
import os
import shutil
import sys
import time

from .. import db


def _backup(db_path: str) -> str:
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"workbench-{stamp}.db")
    shutil.copy2(db_path, dest)
    return dest


def plan() -> dict:
    """计算回填方案，不做任何写入。"""
    rows = db.query("SELECT id,name,owner_id,tenant_id,organization_id FROM projects "
                    "WHERE organization_id IS NULL OR organization_id=''")
    resolved, unresolved = [], []
    for p in rows:
        owner = p["owner_id"]
        reason = None
        target = None
        if not owner:
            reason = "项目没有 owner_id"
        else:
            u = db.query_one("SELECT id,name,organization_id FROM users WHERE id=?", (owner,))
            if not u:
                reason = f"owner（{owner}）不存在"
            else:
                orgs = [m["organization_id"] for m in
                        db.query("SELECT DISTINCT organization_id FROM memberships WHERE user_id=? AND organization_id IS NOT NULL",
                                 (owner,))]
                orgs = [o for o in orgs if o]
                if len(orgs) == 1:
                    target = orgs[0]
                elif len(orgs) == 0 and u.get("organization_id"):
                    target = u["organization_id"]
                elif len(orgs) > 1:
                    reason = f"owner（{owner}）同属 {len(orgs)} 个组织：{'、'.join(orgs)}"
                else:
                    reason = f"owner（{owner}）没有任何组织归属"
                if target and not db.query_one("SELECT id FROM organizations WHERE id=?", (target,)):
                    reason = f"推断出的组织 {target} 不存在"
                    target = None
        item = {"project_id": p["id"], "name": p["name"], "owner_id": owner,
                "tenant_id": p["tenant_id"], "organization_id": target, "reason": reason}
        (resolved if target else unresolved).append(item)
    return {"scanned": len(rows), "resolved": resolved, "unresolved": unresolved}


def run(dry_run: bool = False) -> dict:
    db.init_db()
    db_path = db.DB_PATH
    result = plan()
    backup = ""
    report_path = ""
    if not dry_run and result["scanned"]:
        backup = _backup(db_path)
        for item in result["resolved"]:
            db.execute("UPDATE projects SET organization_id=? WHERE id=? AND (organization_id IS NULL OR organization_id='')",
                       (item["organization_id"], item["project_id"]))
        report_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, f"backfill-report-{time.strftime('%Y%m%d-%H%M%S')}.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({"backup": backup, **result}, f, ensure_ascii=False, indent=2)
        now = int(db.now())
        db.execute("INSERT INTO migrations(id,name,started_at,finished_at,scanned,resolved,unresolved,report_path,detail_json) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (db.gen_id("mig"), "backfill_project_orgs", now, now, result["scanned"],
                    len(result["resolved"]), len(result["unresolved"]), report_path,
                    json.dumps(result["unresolved"], ensure_ascii=False)))
    return {**result, "backup": backup, "report_path": report_path, "dry_run": dry_run}


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    r = run(dry_run=dry)
    print(f"扫描 {r['scanned']} 个归属不明的项目；可自动回填 {len(r['resolved'])} 个；待人工处理 {len(r['unresolved'])} 个")
    for it in r["resolved"]:
        print(f"  [回填] {it['project_id']}  {it['name']}  owner={it['owner_id']} → {it['organization_id']}")
    for it in r["unresolved"]:
        print(f"  [待定] {it['project_id']}  {it['name']}  原因：{it['reason']}")
    if r["backup"]:
        print(f"数据库备份：{r['backup']}")
        print(f"迁移报告：{r['report_path']}")
    elif dry:
        print("（dry-run，未写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
