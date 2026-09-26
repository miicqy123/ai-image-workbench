"""站内通知：任务完成 / 失败、审核结果等统一入口。"""
from .. import db

DEFAULT_USER = "usr_default"


def notify(user_id: str, ntype: str, title: str, body: str = "",
           ref_type: str = "", ref_id: str = "") -> str:
    nid = db.gen_id("ntf")
    db.execute("INSERT INTO notifications(id,user_id,type,title,body,ref_type,ref_id,read,created_at) "
               "VALUES(?,?,?,?,?,?,?,0,?)",
               (nid, user_id or DEFAULT_USER, ntype, title, body, ref_type or "", ref_id or "", int(db.now())))
    return nid
