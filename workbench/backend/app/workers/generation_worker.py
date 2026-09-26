"""生图任务 worker：领取 queued 任务并执行。

运行：python -m app.workers.generation_worker
"""
import json
import time
from .. import db, registry, storage
from ..generators import image as image_gen


def _claim_one():
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM generation_jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1").fetchone()
        if not row:
            return None
        job = dict(row)
        cur = conn.execute("UPDATE generation_jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                           (int(db.now()), job["id"]))
        if cur.rowcount != 1:
            return None
        return job


def _run_job(job):
    brief = db.query_one("SELECT * FROM generation_briefs WHERE id=?", (job["brief_id"],))
    if not brief:
        raise ValueError("brief 不存在")
    model_id = job["model_id"]
    m = registry.get_model(model_id) or {}
    provider_cfg = registry.resolve_provider(model_id)
    product_ids = json.loads(brief["product_asset_ids_json"] or "[]")
    ref_ids = json.loads(brief["reference_asset_ids_json"] or "[]")
    pil_imgs = []
    for aid in list(product_ids) + list(ref_ids):
        a = db.query_one("SELECT object_key FROM assets WHERE id=?", (aid,))
        if a:
            try:
                pil_imgs.append(storage.read_pillow(a["object_key"]))
            except Exception:
                pass
    req = {"model_id": model_id, "prompt": brief["user_prompt"], "negative_prompt": "",
           "aspect_ratio": brief["aspect_ratio"], "count": brief["image_count"],
           "task_type": job["task_type"], "params": {}}
    res = image_gen.dispatch(model_id, req, pil_imgs, provider_cfg=provider_cfg, adapter=m.get("adapter"))
    outputs = res.get("outputs", [])
    for o in outputs:
        aid = db.gen_id("ast")
        obj_key = f"{brief['project_id']}/{aid}.png"
        storage.save_bytes(obj_key, o["bytes"])
        db.execute("INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (aid, "tnt_default", brief["project_id"], "image", "generated", obj_key, "", "image/png",
                    o["width"], o["height"], db.now(), "generation_job", model_id, "generated"))
        db.execute("INSERT INTO generation_candidates(id,project_id,job_id,prompt_version_id,asset_id,model_id,provider_id,width,height,status,is_selected,metadata_json,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (db.gen_id("cand"), brief["project_id"], job["id"], job.get("prompt_version_id"), aid, model_id,
                    job.get("provider_id"), o["width"], o["height"], "ready", 0, "{}", int(db.now())))
    return len(outputs)


def run_once():
    job = _claim_one()
    if not job:
        return False
    try:
        _run_job(job)
        db.execute("UPDATE generation_jobs SET status='succeeded', progress=100, finished_at=? WHERE id=?",
                   (int(db.now()), job["id"]))
    except Exception as e:
        db.execute("UPDATE generation_jobs SET status='failed', error_code=?, error_message=?, finished_at=? WHERE id=?",
                   (type(e).__name__, str(e)[:300], int(db.now()), job["id"]))
    return True


def main():
    while True:
        if not run_once():
            time.sleep(2)


if __name__ == "__main__":
    main()
