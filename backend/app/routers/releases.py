from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import POST_ADMIT_STATUSES, db, db_immediate, rows_to_list, row_to_dict
from app.services.common import (
    RELEASE_SELECT,
    assert_app_enabled,
    audit,
    get_release,
    get_setting,
    next_serial,
    now_str,
)

router = APIRouter(tags=["发版单"])


class ReleaseIn(BaseModel):
    应用编号: int
    目标版本: str = Field(min_length=1)
    制品摘要: str = ""
    金丝雀比例: int | None = None
    备注: str = ""


class PipelineIn(BaseModel):
    流水线: str


@router.get("/发版单")
def list_releases():
    with db() as conn:
        return rows_to_list(conn.execute(RELEASE_SELECT + " ORDER BY r.id DESC").fetchall())


@router.get("/发版单/{release_id}")
def one_release(release_id: int):
    with db() as conn:
        return row_to_dict(get_release(conn, release_id))


@router.post("/发版单")
def create_release(body: ReleaseIn):
    with db() as conn:
        assert_app_enabled(conn, body.应用编号)
        ratio = body.金丝雀比例
        if ratio is None:
            ratio = int(get_setting(conn, "金丝雀默认比例", "10") or "10")
        if ratio < 0 or ratio > 100:
            raise HTTPException(400, "金丝雀比例须在 0 到 100")
        serial = next_serial(conn)
        now = now_str()
        cur = conn.execute(
            """
            INSERT INTO releases(
              serial, app_id, target_version, artifact_digest, status, pipeline_status,
              canary_ratio, note, created_at
            ) VALUES (?, ?, ?, ?, '待入闸', '未回执', ?, ?, ?)
            """,
            (serial, body.应用编号, body.目标版本, body.制品摘要, ratio, body.备注, now),
        )
        rid = cur.lastrowid
        audit(conn, "发版单", rid, "申请发版", serial)
        return row_to_dict(get_release(conn, rid))


@router.post("/发版单/{release_id}/入闸")
def admit(release_id: int):
    with db() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        if r["status"] != "待入闸":
            raise HTTPException(400, "仅待入闸可入闸")
        assert_app_enabled(conn, r["app_id"])
        must_green = get_setting(conn, "入闸须流水线绿", "是") == "是"
        if must_green and r["pipeline_status"] != "绿":
            raise HTTPException(400, "流水线未绿，禁止入闸")
        if not r["artifact_digest"]:
            raise HTTPException(400, "缺少制品摘要，禁止入闸")
        active = conn.execute(
            """
            SELECT id FROM releases
            WHERE app_id=? AND id!=? AND status IN ('已入闸','晋升中','已放行','回滚中')
            """,
            (r["app_id"], release_id),
        ).fetchone()
        if active:
            raise HTTPException(400, "同应用已有在途发版单，禁止并行入闸")
        test = conn.execute("SELECT id FROM environments WHERE code='测'").fetchone()
        now = now_str()
        # 入闸快照：以入闸当时的流水线结论作数，冻结标此时必须为 0
        conn.execute(
            """
            UPDATE releases
            SET status='已入闸', admitted_at=?, current_env_id=?,
                pipeline_snapshot_at_admit=?, frozen=0, red_after_admit_at=NULL
            WHERE id=?
            """,
            (now, test["id"], r["pipeline_status"], release_id),
        )
        audit(
            conn, "发版单", release_id, "入闸",
            f"{r['serial']} 入闸快照流水线={r['pipeline_status']}",
        )
        return row_to_dict(get_release(conn, release_id))


@router.post("/发版单/{release_id}/流水线")
def set_pipeline(release_id: int, body: PipelineIn):
    if body.流水线 not in ("未回执", "绿", "红"):
        raise HTTPException(400, "流水线状态非法")
    # 写口取写锁：与晋升写口互斥，并发翻红/晋升只有一个结局
    with db_immediate() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        now = now_str()
        if r["status"] not in POST_ADMIT_STATUSES:
            # 入闸前的红绿只影响入闸当下红绿闸（另立），不产生冻结
            conn.execute(
                "UPDATE releases SET pipeline_status=? WHERE id=?",
                (body.流水线, release_id),
            )
            audit(conn, "发版单", release_id, "流水线回执", body.流水线)
            return row_to_dict(get_release(conn, release_id))

        # —— 入闸后：翻红即冻结（臂一，fail-closed）——
        if body.流水线 == "红":
            already_frozen = bool(r["frozen"])
            # 冻结标粘性：首次翻红记录时间戳；重复翻红不覆盖首次时间
            conn.execute(
                """
                UPDATE releases
                SET pipeline_status='红', frozen=1,
                    red_after_admit_at=COALESCE(red_after_admit_at, ?)
                WHERE id=?
                """,
                (now, release_id),
            )
            if already_frozen:
                audit(conn, "发版单", release_id, "入闸后翻红", f"重复翻红维持冻结@{now}")
            else:
                audit(
                    conn, "发版单", release_id, "入闸后翻红",
                    f"事后翻红冻结@{now}，入闸快照={r['pipeline_snapshot_at_admit']}",
                )
        else:
            # 翻绿/未回执只改流水线灯色；冻结标粘性，不因翻绿而解除
            conn.execute(
                "UPDATE releases SET pipeline_status=? WHERE id=?",
                (body.流水线, release_id),
            )
            if r["frozen"]:
                audit(
                    conn, "发版单", release_id, "流水线回执",
                    f"{body.流水线}（冻结维持，不得向更高环境推进）",
                )
            else:
                audit(conn, "发版单", release_id, "流水线回执", body.流水线)
        return row_to_dict(get_release(conn, release_id))
