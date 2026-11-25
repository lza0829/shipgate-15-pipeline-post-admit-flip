"""
入闸后翻红命运——臂一「翻红即冻结」夹具测试。
不依赖 pytest / httpx，直接驱动路由函数（每个写口各自开连接，支持线程并发）。

覆盖：
  1. 入闸后打红再晋升：整笔拒绝 + 冻结标 + 翻红时间戳 + 快照 + 拒因 + 审计/总览/对账串查
  2. 绿单晋升：测→预→产 全程放行，不冻结
  3. 并发翻红 vs 晋升：写锁串行，只有单结局，绝不无痕迹晋升高环境
  4. 半截脏晋升：中途异常整笔回滚收口，无残留高环境版本
  5. 读侧可重放：翻红后反复晋升，结论一致可重放
  6. 冻结粘性：翻红后再翻绿仍冻结
  7. 与入闸当下红绿闸错开：入闸前翻红不产生冻结
  8. 旧库增量迁移：缺列老库可平滑补齐
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_FD, DB_PATH = tempfile.mkstemp(prefix="sg_fixture_", suffix=".db")
os.close(DB_FD)
os.remove(DB_PATH)
os.environ["SHIPGATE_DB"] = DB_PATH

from fastapi import HTTPException  # noqa: E402

from app.db import connect, db, init_db  # noqa: E402
from app.routers import dashboard, promote, reconcile, releases, audit  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services.common import now_str  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def expect_http(name: str, fn, status: int = 400) -> HTTPException | None:
    try:
        fn()
    except HTTPException as e:
        check(name, e.status_code == status, f"got {e.status_code} want {status}")
        return e
    check(name, False, "未抛出 HTTPException")
    return None


def make_admitted_green() -> int:
    """申请→流水线绿→入闸，返回已入闸发版单 id（落地测环境）。"""
    close_inflight(2)
    rid = releases.create_release(
        releases.ReleaseIn(应用编号=2, 目标版本="9.9.9", 制品摘要="sha256:fixture")
    )["编号"]
    releases.set_pipeline(rid, releases.PipelineIn(流水线="绿"))
    releases.admit(rid)
    return rid


def get_raw(rid: int) -> sqlite3.Row:
    with db() as conn:
        return conn.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()


def close_inflight(app_id: int = 2) -> None:
    """把指定应用仍在途的单收口为已关闭，避免占用“同应用并行入闸”名额。
    只改状态，审计/晋升/冻结痕迹均保留。"""
    with db() as conn:
        conn.execute(
            "UPDATE releases SET status='已关闭' WHERE app_id=? "
            "AND status IN ('已入闸','晋升中','已放行','回滚中')",
            (app_id,),
        )


# ---------------------------------------------------------------------------
def fixture_red_after_admit_then_promote():
    print("\n[夹具1] 入闸后打红再晋升（翻红即冻结，整笔拒绝，五处串查）")
    rid = make_admitted_green()
    before = get_raw(rid)
    check("入闸快照流水线=绿", before["pipeline_snapshot_at_admit"] == "绿")
    check("入闸时未冻结", before["frozen"] == 0)
    check("入闸落地测环境", before["current_env_id"] is not None)

    # 入闸后翻红
    red_resp = releases.set_pipeline(rid, releases.PipelineIn(流水线="红"))
    check("发版单流水线区灯色=红", red_resp["流水线"] == "红")
    check("发版单露出冻结标", red_resp["冻结"] == 1)
    check("发版单露事后翻红时间戳", bool(red_resp["入闸后翻红时间"]))
    check("入闸快照仍保留为绿", red_resp["入闸流水线快照"] == "绿")

    raw = get_raw(rid)
    check("库内冻结粘性位=1", raw["frozen"] == 1)
    check("库内翻红时间戳已写", bool(raw["red_after_admit_at"]))
    frozen_at = raw["red_after_admit_at"]

    # 晋升整笔失败
    e = expect_http("翻红后晋升被整笔拒绝", lambda: promote.promote(
        rid, promote.PromoteIn(目标环境="预")))
    check("拒因可读且点明冻结", e is not None and "冻结" in e.detail)

    raw2 = get_raw(rid)
    check("拒绝后仍停在测环境，未推进", raw2["current_env_id"] == raw["current_env_id"])
    check("拒绝后状态未变", raw2["status"] == raw["status"])
    check("拒绝后冻结维持", raw2["frozen"] == 1 and raw2["red_after_admit_at"] == frozen_at)

    # 晋升写口留痕：promote_events 一条“拒绝”
    with db() as conn:
        ev = conn.execute(
            "SELECT * FROM promote_events WHERE release_id=? ORDER BY id DESC LIMIT 1", (rid,)
        ).fetchone()
    check("promote_events 记录拒绝", ev["decision"] == "拒绝")
    check("拒绝事件带拒因", "冻结" in ev["note"])

    # 总览：在途不消失，且露出冻结
    ov = dashboard.overview()
    in_flight = [r for r in ov["在途"] if r["编号"] == rid]
    check("总览在途仍保留冻结单（不消失）", len(in_flight) == 1)
    check("总览在途格露出冻结标", in_flight and in_flight[0]["冻结"] == 1)
    check("总览在途格露出翻红时间", in_flight and bool(in_flight[0]["入闸后翻红时间"]))
    check("总览冻结数计数", ov["冻结数"] >= 1)

    # 对账旁读
    rc = reconcile.reconcile()
    fl = [r for r in rc["冻结清单"] if r["编号"] == rid]
    check("对账冻结清单可核", len(fl) == 1 and fl[0]["入闸流水线快照"] == "绿")
    rj = [r for r in rc["晋升拒绝清单"] if r["发版单号"] == raw2["serial"]]
    check("对账晋升拒绝清单带拒因", len(rj) >= 1 and all("冻结" in x["拒因"] for x in rj))
    check("对账晋升拒绝数增加", rc["晋升拒绝数"] >= 1 and rc["冻结数"] >= 1)

    # 审计：翻红证据 + 晋升拒因同刷
    au = audit.list_audit()
    kinds = [(a["动作"], a["备注"]) for a in au if a["关联编号"] == rid]
    check("审计含入闸后翻红记录", any("入闸后翻红" in a for a, _ in kinds))
    check("审计翻红记录带快照与时间", any("事后翻红冻结" in n and "快照=绿" in n for _, n in kinds))
    check("审计含晋升拒绝记录", any("晋升拒绝" in a for a, _ in kinds))


def fixture_green_promote():
    print("\n[夹具2] 绿单晋升（测→预→产，全程放行不冻结）")
    rid = make_admitted_green()
    p1 = promote.promote(rid, promote.PromoteIn(目标环境="预"))
    check("绿单晋升到预成功", p1["当前环境"] == "预" and p1["状态"] == "晋升中")
    check("晋升后未冻结", p1["冻结"] == 0)
    p2 = promote.promote(rid, promote.PromoteIn(目标环境="产"))
    check("绿单晋升到产已放行", p2["当前环境"] == "产" and p2["状态"] == "已放行")
    check("全程冻结标为0", get_raw(rid)["frozen"] == 0)
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM promote_events WHERE release_id=? AND decision='放行'", (rid,)
        ).fetchone()["c"]
    check("两条放行事件留痕", n == 2)


def fixture_concurrent_red_and_promote():
    print("\n[夹具3] 并发翻红 vs 晋升（写锁串行，单结局，无痕迹高环境晋升为零）")
    iterations = 12
    trace_free_leak = 0
    double_outcome = 0
    for i in range(iterations):
        rid = make_admitted_green()
        barrier = threading.Barrier(2)
        results = {}

        def turn_red():
            barrier.wait()
            try:
                releases.set_pipeline(rid, releases.PipelineIn(流水线="红"))
                results["red"] = "ok"
            except Exception as ex:  # noqa: BLE001
                results["red"] = repr(ex)

        def turn_promote():
            barrier.wait()
            try:
                promote.promote(rid, promote.PromoteIn(目标环境="预"))
                results["promote"] = "放行"
            except HTTPException:
                results["promote"] = "拒绝"
            except Exception as ex:  # noqa: BLE001
                results["promote"] = repr(ex)

        t1 = threading.Thread(target=turn_red)
        t2 = threading.Thread(target=turn_promote)
        t1.start(); t2.start(); t1.join(); t2.join()

        raw = get_raw(rid)
        check_lock = results.get("red") == "ok"
        with db() as conn:
            allowed = conn.execute(
                "SELECT COUNT(*) c FROM promote_events WHERE release_id=? AND decision='放行'", (rid,)
            ).fetchone()["c"]
            rejected = conn.execute(
                "SELECT COUNT(*) c FROM promote_events WHERE release_id=? AND decision='拒绝'", (rid,)
            ).fetchone()["c"]

        # 翻红必须最终落库（写锁保证 red 不丢）
        if not check_lock or raw["frozen"] != 1 or raw["pipeline_status"] != "红":
            double_outcome += 1
        # 至多一次放行；若放行则必有完整留痕，绝不允许“无痕迹”高环境晋升
        if allowed > 1:
            trace_free_leak += 1
        if allowed == 1 and results.get("promote") != "放行":
            trace_free_leak += 1  # 有放行却无对应结局=双结局
        # 若晋升被拒，必须有拒绝留痕
        if results.get("promote") == "拒绝" and rejected < 1:
            trace_free_leak += 1
        # 冻结后再试一次，必拒（读侧可重放的并发侧面）
        try:
            promote.promote(rid, promote.PromoteIn(目标环境="预"))
            trace_free_leak += 1  # 已冻结居然还能推高环境=泄漏
        except HTTPException:
            pass

    check(f"{iterations} 轮并发翻红均落库冻结（无丢红/双结局）", double_outcome == 0, f"异常 {double_outcome} 轮")
    check("并发后无无痕迹高环境晋升、拒绝均留痕", trace_free_leak == 0, f"泄漏 {trace_free_leak} 轮")


def fixture_half_dirty_promote():
    print("\n[夹具4] 半截脏晋升（中途异常整笔回滚收口）")
    rid = make_admitted_green()
    raw_before = get_raw(rid)
    app_id, ver, target = raw_before["app_id"], raw_before["target_version"], "预"
    with db() as conn:
        env_pre = conn.execute("SELECT id FROM environments WHERE code='预'").fetchone()["id"]
        before_rows = conn.execute(
            "SELECT COUNT(*) c FROM app_env_versions WHERE app_id=? AND env_id=? AND version=?",
            (app_id, env_pre, ver),
        ).fetchone()["c"]

    original = promote.upsert_instance
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("模拟晋升半截故障")

    promote.upsert_instance = boom
    raised = False
    try:
        promote.promote(rid, promote.PromoteIn(目标环境=target))
    except Exception as ex:  # noqa: BLE001 半截故障以任意异常收口即可
        raised = True
        check("半截故障时晋升报错（非 HTTP 业务异常也收口）", isinstance(ex, RuntimeError))
    finally:
        promote.upsert_instance = original
    check("半截故障确实令晋升中断", raised)

    check("故障注入确实触发", calls["n"] == 1)
    raw_after = get_raw(rid)
    check("发版单状态/环境回滚未推进", raw_after["current_env_id"] == raw_before["current_env_id"]
          and raw_after["status"] == raw_before["status"])
    with db() as conn:
        after_rows = conn.execute(
            "SELECT COUNT(*) c FROM app_env_versions WHERE app_id=? AND env_id=? AND version=?",
            (app_id, env_pre, ver),
        ).fetchone()["c"]
        allow_n = conn.execute(
            "SELECT COUNT(*) c FROM promote_events WHERE release_id=? AND decision='放行'", (rid,)
        ).fetchone()["c"]
        # 故障注入点就是 upsert_instance 本身，它在预环境没有任何写入机会；
        # 按发版单编号核查预环境，排除该单在测环境的既有实例干扰
        inst_n = conn.execute(
            "SELECT COUNT(*) c FROM instances WHERE app_id=? AND env_id=? AND release_id=?",
            (app_id, env_pre, rid),
        ).fetchone()["c"]
    check("名册无半截高环境版本残留", after_rows == before_rows)
    check("无放行事件残留（整笔回滚）", allow_n == 0)
    check("预环境无本单实例半截写入", inst_n == 0)

    # 收口后该单仍可正常晋升（无脏锁）
    ok = promote.promote(rid, promote.PromoteIn(目标环境="预"))
    check("收口后绿单可正常晋升", ok["当前环境"] == "预")


def fixture_replayable_readside():
    print("\n[夹具5] 读侧可重放（翻红后反复晋升结论一致）")
    rid = make_admitted_green()
    releases.set_pipeline(rid, releases.PipelineIn(流水线="红"))
    reasons = set()
    for _ in range(5):
        e = expect_http("重放晋升均被拒", lambda: promote.promote(
            rid, promote.PromoteIn(目标环境="预")))
        if e:
            reasons.add(e.detail)
    check("每次拒因一致（可重放）", len(reasons) == 1 and "冻结" in next(iter(reasons)))
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM promote_events WHERE release_id=? AND decision='拒绝'", (rid,)
        ).fetchone()["c"]
    check("5 次拒绝全部留痕", n == 5)
    check("重放不改变环境", get_raw(rid)["current_env_id"] is not None
          and get_raw(rid)["status"] == "已入闸")


def fixture_sticky_freeze():
    print("\n[夹具6] 冻结粘性（翻红后再翻绿仍冻结）")
    rid = make_admitted_green()
    releases.set_pipeline(rid, releases.PipelineIn(流水线="红"))
    ts1 = get_raw(rid)["red_after_admit_at"]
    releases.set_pipeline(rid, releases.PipelineIn(流水线="绿"))
    raw = get_raw(rid)
    check("翻绿后灯色变绿", raw["pipeline_status"] == "绿")
    check("翻绿后冻结标不解除", raw["frozen"] == 1)
    check("首次翻红时间戳不被覆盖", raw["red_after_admit_at"] == ts1)
    expect_http("翻绿后仍禁止晋升", lambda: promote.promote(
        rid, promote.PromoteIn(目标环境="预")))
    # 再次翻红：时间戳维持首次
    releases.set_pipeline(rid, releases.PipelineIn(流水线="红"))
    check("重复翻红维持首次时间戳", get_raw(rid)["red_after_admit_at"] == ts1)


def fixture_pre_admit_separate_gate():
    print("\n[夹具7] 与入闸当下红绿闸错开（入闸前翻红不冻结）")
    close_inflight(2)
    rid = releases.create_release(
        releases.ReleaseIn(应用编号=2, 目标版本="8.8.8", 制品摘要="sha256:pre")
    )["编号"]
    releases.set_pipeline(rid, releases.PipelineIn(流水线="红"))
    check("入闸前翻红冻结标仍为0", get_raw(rid)["frozen"] == 0)
    check("入闸前翻红无翻红时间戳", get_raw(rid)["red_after_admit_at"] is None)
    # 入闸当下红绿闸另立：红单不准入闸
    expect_http("红单入闸被当下闸口拦截", lambda: releases.admit(rid))
    releases.set_pipeline(rid, releases.PipelineIn(流水线="绿"))
    releases.admit(rid)
    check("绿单入闸成功且快照=绿", get_raw(rid)["pipeline_snapshot_at_admit"] == "绿")


def fixture_legacy_migration():
    print("\n[夹具8] 旧库增量迁移（缺列老库平滑补齐）")
    old_fd, old_path = tempfile.mkstemp(prefix="sg_legacy_", suffix=".db")
    os.close(old_fd)
    old_conn = sqlite3.connect(old_path)
    old_conn.executescript(
        """
        CREATE TABLE apps(id INTEGER PRIMARY KEY, name TEXT UNIQUE, owner TEXT DEFAULT '',
          status TEXT DEFAULT '启用');
        CREATE TABLE environments(id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, sort_order INT);
        CREATE TABLE releases(
          id INTEGER PRIMARY KEY, serial TEXT UNIQUE, app_id INT, target_version TEXT,
          artifact_digest TEXT DEFAULT '', status TEXT DEFAULT '待入闸',
          pipeline_status TEXT DEFAULT '未回执', current_env_id INT, canary_ratio INT DEFAULT 10,
          note TEXT DEFAULT '', created_at TEXT, admitted_at TEXT, released_at TEXT,
          rolled_back_at TEXT);
        INSERT INTO environments(code,name,sort_order) VALUES ('测','测试',1),('预','预发',2),('产','生产',3);
        INSERT INTO apps(name) VALUES ('老应用');
        INSERT INTO releases(serial,app_id,target_version,status,pipeline_status,created_at)
          VALUES ('发202601010009',1,'1.0.0','已入闸','绿','2026-01-01 00:00:00');
        """
    )
    old_conn.commit(); old_conn.close()
    # config.DB_PATH 在导入时已固定，运行中改环境变量无效；直接 monkeypatch 路径并重置 WAL 守卫
    from app import config as app_config
    from app import db as db_module
    original_db_path = app_config.DB_PATH
    app_config.DB_PATH = type(app_config.DB_PATH)(old_path)
    db_module.DB_PATH = app_config.DB_PATH
    db_module._WAL_INITIALIZED.discard(str(app_config.DB_PATH))
    try:
        init_db()  # 触发迁移
        c = sqlite3.connect(old_path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(releases)").fetchall()}
        check("迁移补出 pipeline_snapshot_at_admit", "pipeline_snapshot_at_admit" in cols)
        check("迁移补出 frozen", "frozen" in cols)
        check("迁移补出 red_after_admit_at", "red_after_admit_at" in cols)
        row = c.execute("SELECT frozen, pipeline_snapshot_at_admit FROM releases WHERE serial='发202601010009'").fetchone()
        check("老数据冻结默认0、快照为空", row[0] == 0 and row[1] is None)
        c.close()
    finally:
        app_config.DB_PATH = original_db_path
        db_module.DB_PATH = original_db_path
        db_module._WAL_INITIALIZED.discard(str(app_config.DB_PATH))
        os.remove(old_path)


def main():
    seed_if_empty()
    fixture_red_after_admit_then_promote()
    fixture_green_promote()
    fixture_concurrent_red_and_promote()
    fixture_half_dirty_promote()
    fixture_replayable_readside()
    fixture_sticky_freeze()
    fixture_pre_admit_separate_gate()
    fixture_legacy_migration()
    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
