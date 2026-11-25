from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list

router = APIRouter(tags=["对账"])


@router.get("/对账")
def reconcile():
    with db() as conn:
        release_stats = {
            "申请数": conn.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"],
            "已放行": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已放行'"
            ).fetchone()["c"],
            "已回滚": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已回滚'"
            ).fetchone()["c"],
            "在途": conn.execute(
                """
                SELECT COUNT(*) AS c FROM releases
                WHERE status IN ('已入闸','晋升中','已放行','回滚中')
                """
            ).fetchone()["c"],
        }
        version_vs_instance = rows_to_list(
            conn.execute(
                """
                SELECT a.name AS 应用, e.code AS 环境,
                       v.version AS 名册版本,
                       GROUP_CONCAT(i.version, ',') AS 实例版本们,
                       SUM(CASE WHEN i.health='探针红' THEN 1 ELSE 0 END) AS 红灯数
                FROM app_env_versions v
                JOIN apps a ON a.id = v.app_id
                JOIN environments e ON e.id = v.env_id
                LEFT JOIN instances i ON i.app_id = v.app_id AND i.env_id = v.env_id
                GROUP BY v.app_id, v.env_id
                ORDER BY a.id, e.sort_order
                """
            ).fetchall()
        )
        promote_count = conn.execute("SELECT COUNT(*) AS c FROM promote_events").fetchone()["c"]
        rejected_count = conn.execute(
            "SELECT COUNT(*) AS c FROM promote_events WHERE decision='拒绝'"
        ).fetchone()["c"]
        frozen_count = conn.execute(
            "SELECT COUNT(*) AS c FROM releases WHERE frozen=1"
        ).fetchone()["c"]
        frozen_list = rows_to_list(
            conn.execute(
                """
                SELECT r.id AS 编号, r.serial AS 发版单号, a.name AS 应用,
                       r.status AS 状态, r.pipeline_status AS 流水线,
                       r.pipeline_snapshot_at_admit AS 入闸流水线快照,
                       r.red_after_admit_at AS 入闸后翻红时间,
                       e.code AS 当前环境
                FROM releases r
                JOIN apps a ON a.id = r.app_id
                LEFT JOIN environments e ON e.id = r.current_env_id
                WHERE r.frozen=1
                ORDER BY r.red_after_admit_at, r.id
                """
            ).fetchall()
        )
        rejection_list = rows_to_list(
            conn.execute(
                """
                SELECT p.id AS 编号, r.serial AS 发版单号, e.code AS 目标环境,
                       p.created_at AS 时间, p.note AS 拒因
                FROM promote_events p
                JOIN releases r ON r.id = p.release_id
                LEFT JOIN environments e ON e.id = p.to_env_id
                WHERE p.decision='拒绝'
                ORDER BY p.id DESC
                """
            ).fetchall()
        )
        return {
            "发版统计": release_stats,
            "晋升事件数": promote_count,
            "晋升拒绝数": rejected_count,
            "冻结数": frozen_count,
            "冻结清单": frozen_list,
            "晋升拒绝清单": rejection_list,
            "名册与实例": version_vs_instance,
        }
