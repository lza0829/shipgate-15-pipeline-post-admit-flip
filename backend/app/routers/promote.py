from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect, db, row_to_dict
from app.services.common import (
    active_release_window,
    audit,
    env_by_code,
    get_release,
    get_setting,
    now_str,
    ordered_envs,
    set_app_env_version,
    upsert_instance,
)

router = APIRouter(tags=["晋升"])


class PromoteIn(BaseModel):
    目标环境: str
    备注: str = ""


def _record_rejection(conn, release_id: int, from_env_id, to_env_id: int, reason: str, note: str) -> None:
    """拒绝也要留痕：promote_events 记一条“拒绝”，审计记晋升拒因。"""
    conn.execute(
        """
        INSERT INTO promote_events(release_id, from_env_id, to_env_id, decision, created_at, note)
        VALUES (?, ?, ?, '拒绝', ?, ?)
        """,
        (release_id, from_env_id, to_env_id, now_str(), reason),
    )
    audit(conn, "晋升", release_id, "晋升拒绝", f"{reason}｜{note}")


@router.post("/发版单/{release_id}/晋升")
def promote(release_id: int, body: PromoteIn):
    # 整笔晋升在单一写锁事务内完成：与翻红写口互斥，杜绝并发双结局与半截脏写。
    # 注意：冻结拒绝属于要留痕的业务结局，先写拒绝事件并 commit，再抛 400，
    # 不能在事务里直接 raise（那会连拒因一起回滚）。
    conn = connect()
    reject_error: HTTPException | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")  # 开启即取写锁，串行化翻红/晋升
        r = conn.execute(
            "SELECT * FROM releases WHERE id=?", (release_id,)
        ).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        if r["status"] not in ("已入闸", "晋升中"):
            raise HTTPException(400, "当前状态不可晋升")
        to_env = env_by_code(conn, body.目标环境)
        envs = ordered_envs(conn)
        codes = [e["code"] for e in envs]
        if body.目标环境 not in codes:
            raise HTTPException(400, "目标环境非法")
        cur_env = None
        if r["current_env_id"]:
            cur_env = conn.execute(
                "SELECT * FROM environments WHERE id=?", (r["current_env_id"],)
            ).fetchone()
        allow_skip = get_setting(conn, "允许跳级", "否") == "是"
        if cur_env:
            cur_idx = codes.index(cur_env["code"])
            to_idx = codes.index(body.目标环境)
            if to_idx <= cur_idx:
                raise HTTPException(400, "只能向更高环境晋升")
            if not allow_skip and to_idx != cur_idx + 1:
                raise HTTPException(400, "禁止跳级，须逐级晋升")
        else:
            if body.目标环境 != "测":
                raise HTTPException(400, "未落地时只能先入测")

        win = active_release_window(conn, to_env["id"])
        hard = get_setting(conn, "维护窗硬拒", "是") == "是"
        if hard and not win:
            raise HTTPException(400, "目标环境放行窗已关，禁止晋升")

        # —— 臂一：翻红即冻结。入闸后冻结（含事后翻红）整笔拒绝，不得向更高环境推进 ——
        # 冻结标与当前灯色双保险：灯色翻红而冻结标因故未置时仍拦截。
        if r["frozen"] or r["pipeline_status"] == "红":
            reason = "流水线入闸后翻红已冻结，禁止向更高环境晋升"
            _record_rejection(
                conn, release_id, r["current_env_id"], to_env["id"], reason, body.备注
            )
            conn.commit()  # 拒因落库后再抛出，确保拒绝可追溯、不被回滚
            reject_error = HTTPException(400, reason)
        else:
            now = now_str()
            conn.execute(
                """
                INSERT INTO promote_events(release_id, from_env_id, to_env_id, decision, created_at, note)
                VALUES (?, ?, ?, '放行', ?, ?)
                """,
                (release_id, r["current_env_id"], to_env["id"], now, body.备注),
            )
            new_status = "已放行" if body.目标环境 == "产" else "晋升中"
            released_at = now if body.目标环境 == "产" else r["released_at"]
            conn.execute(
                """
                UPDATE releases SET status=?, current_env_id=?, released_at=? WHERE id=?
                """,
                (new_status, to_env["id"], released_at, release_id),
            )
            set_app_env_version(conn, r["app_id"], to_env["id"], r["target_version"])
            upsert_instance(conn, r["app_id"], to_env["id"], r["target_version"], "探针绿", release_id)
            audit(conn, "晋升", release_id, f"晋升到{body.目标环境}", body.备注)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if reject_error is not None:
        raise reject_error
    # 锁已释放，用独立短连接读回结果
    with db() as c:
        return row_to_dict(get_release(c, release_id))
