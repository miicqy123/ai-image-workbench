"""计量与额度：把每次模型调用记成用量记录，并扣减组织额度。

设计要点：
- 只有成功的调用才计费；失败调用记录为失败成本 0，便于后台统计失败率与失败成本。
- 单价来自 model_registry.unit_cost（后台可配置），成本 = 单价 x 计费张数。
- 额度变动统一走 credit_ledger，可追溯、可人工补发 / 扣除 / 退款。
"""
from .. import db, registry

DEFAULT_ORG = "org_default"
DEFAULT_USER = "usr_default"


def unit_cost_for(model_id: str) -> float:
    m = registry.get_model(model_id) or {}
    try:
        return float(m.get("unit_cost") or 0)
    except (TypeError, ValueError):
        return 0.0


def get_org(organization_id: str | None = None) -> dict | None:
    if organization_id:
        o = db.query_one("SELECT * FROM organizations WHERE id=?", (organization_id,))
        if o:
            return o
    return db.query_one("SELECT * FROM organizations WHERE id=?", (DEFAULT_ORG,))


def apply_credit_change(organization_id, change, reason, operator_id="system",
                        ref_type="", ref_id="", user_id=None) -> dict | None:
    """写一条额度流水并更新余额；change 为负数表示消耗/扣除。"""
    o = get_org(organization_id)
    if not o:
        return None
    after = round(float(o["credit_balance"] or 0) + float(change), 4)
    db.execute("UPDATE organizations SET credit_balance=? WHERE id=?", (after, o["id"]))
    lid = db.gen_id("led")
    db.execute("INSERT INTO credit_ledger(id,organization_id,user_id,change,balance_after,reason,operator_id,ref_type,ref_id,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (lid, o["id"], user_id or DEFAULT_USER, round(float(change), 4), after,
                reason or "", operator_id or "system", ref_type or "", ref_id or "", int(db.now())))
    return {"id": lid, "organization_id": o["id"], "change": round(float(change), 4), "balance_after": after}


def record_usage(*, project_id=None, model_id="", task_type="", output_images=0, input_images=0,
                 width=None, height=None, resolution_tier=None, duration_ms=None,
                 status="succeeded", job_id=None, run_id=None, organization_id=None,
                 user_id=None, note="", billed_images=None) -> dict:
    """记录一次调用的用量；成功且单价>0 时自动扣额度。"""
    unit = unit_cost_for(model_id)
    billed = int(output_images or 0) if billed_images is None else int(billed_images or 0)
    cost = round(unit * max(billed, 0), 4) if status == "succeeded" else 0.0
    tenant = None
    if project_id:
        tenant = (db.query_one("SELECT tenant_id FROM projects WHERE id=?", (project_id,)) or {}).get("tenant_id")
    uid = db.gen_id("usg")
    db.execute("INSERT INTO usage_records(id,tenant_id,organization_id,project_id,user_id,job_id,run_id,model_id,task_type,"
               "input_images,output_images,width,height,resolution_tier,duration_ms,unit_cost,cost,status,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (uid, tenant, organization_id or DEFAULT_ORG, project_id, user_id or DEFAULT_USER, job_id, run_id,
                model_id, task_type, int(input_images or 0), int(output_images or 0), width, height,
                resolution_tier, duration_ms, unit, cost, status, int(db.now())))
    if cost > 0:
        apply_credit_change(organization_id, -cost, note or f"{task_type or 'model_call'} 消耗",
                            ref_type="usage", ref_id=uid, user_id=user_id or DEFAULT_USER)
    return {"id": uid, "cost": cost, "unit_cost": unit, "status": status, "billed_images": billed}
