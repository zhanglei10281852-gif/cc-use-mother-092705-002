from __future__ import annotations

from app.database import close_connection
from app.recycled.service import RecycledWaterService

DAY = "2026-09-29"


def reading(system="RW-1", body="东湖", stage="深度处理", volume=100.0,
            start=f"{DAY}T00:00:00+00:00", end=f"{DAY}T01:00:00+00:00", key=None, **extra):
    payload = {
        "source_system": system, "water_body": body, "process_stage": stage,
        "period_start": start, "period_end": end, "volume_m3": volume,
    }
    if key:
        payload["reading_key"] = key
    payload.update(extra)
    return payload


def test_duplicate_report_is_idempotent(client):
    payload = reading(key="dup-1")
    first = client.post("/api/recycled-water/readings", json=payload)
    assert first.status_code == 201, first.text
    second = client.post("/api/recycled-water/readings", json=payload)
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["received_at"] == first.json()["received_at"]

    # 同键但内容不同（维护期重发）仍返回原记录，不产生第二条
    changed = {**payload, "volume_m3": 999.0}
    third = client.post("/api/recycled-water/readings", json=changed)
    assert third.json()["id"] == first.json()["id"]
    assert third.json()["volume_m3"] == 100.0

    listing = client.get("/api/recycled-water/readings", params={"usage_date": DAY}).json()["items"]
    assert len(listing) == 1


def test_cross_day_period_is_rejected_and_must_split(client):
    payload = reading(start=f"{DAY}T23:30:00+00:00", end="2026-09-30T00:30:00+00:00", key="cross-1")
    response = client.post("/api/recycled-water/readings", json=payload)
    assert response.status_code == 422
    assert "自然日边界" in response.json()["error"]["message"]

    # 拆成两段后均可上报
    part1 = reading(start=f"{DAY}T23:30:00+00:00", end=f"{DAY}T23:59:59+00:00", key="cross-1-a", volume=30)
    part2 = reading(start="2026-09-30T00:00:00+00:00", end="2026-09-30T00:30:00+00:00",
                    key="cross-1-b", volume=20)
    assert client.post("/api/recycled-water/readings", json=part1).status_code == 201
    assert client.post("/api/recycled-water/readings", json=part2).status_code == 201


def test_abnormal_reading_goes_to_pending_review_not_dropped(client):
    # 负值（回退/抄表异常）
    negative = client.post("/api/recycled-water/readings",
                           json=reading(volume=-50, key="neg-1")).json()
    assert negative["status"] == "pending_review"
    assert "为负" in negative["flagged_reason"]

    # 离群值：同系统同环节当天先有若干正常读数
    for i in range(3):
        client.post("/api/recycled-water/readings",
                    json=reading(system="RW-2", stage="湿地净化", volume=100 + i, key=f"norm-{i}"))
    outlier = client.post("/api/recycled-water/readings",
                          json=reading(system="RW-2", stage="湿地净化", volume=9999, key="out-1")).json()
    assert outlier["status"] == "pending_review"
    assert "中位值" in outlier["flagged_reason"]

    pending = client.get("/api/recycled-water/readings/pending-review").json()["items"]
    assert {item["reading_key"] for item in pending} == {"neg-1", "out-1"}

    # 待核查读数不进汇总、不进预览
    preview = client.get("/api/recycled-water/daily-report/preview",
                         params={"water_body": "东湖", "usage_date": DAY}).json()
    assert set(preview["pending_reading_ids"]) == {negative["id"], outlier["id"]}
    # 待核查的两条不计入；三条正常读数计入
    assert preview["total_volume_m3"] == 303.0
    preview_stage = next(s for s in preview["stages"] if s["process_stage"] == "湿地净化")
    assert [s["source_system"] for s in preview_stage["sources"]] == ["RW-2"] * 3


def test_review_flow_confirm_and_reject(client):
    neg = client.post("/api/recycled-water/readings",
                      json=reading(volume=-50, key="rv-neg")).json()
    out = client.post("/api/recycled-water/readings",
                      json=reading(system="RW-3", volume=120, key="rv-out")).json()
    # 直接把第二条也置为待核查（用负值后再改不现实，这里再制造一个负值）
    out2 = client.post("/api/recycled-water/readings",
                       json=reading(system="RW-3", volume=-5, key="rv-out2")).json()

    # 非法状态流转
    ok = client.post(f"/api/recycled-water/readings/{out['id']}/review",
                     json={"decision": "confirmed", "reviewer": "zhang"})
    # out 是 accepted，不允许核查
    assert ok.status_code == 409

    confirmed = client.post(f"/api/recycled-water/readings/{neg['id']}/review",
                            json={"decision": "confirmed", "reviewer": "zhang", "reason": "维护期回退属实"})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["reviewer"] == "zhang"

    rejected = client.post(f"/api/recycled-water/readings/{out2['id']}/review",
                           json={"decision": "rejected", "reviewer": "li", "reason": "仪表故障"})
    assert rejected.json()["status"] == "rejected"

    # 已处理的不能再次处理
    again = client.post(f"/api/recycled-water/readings/{neg['id']}/review",
                        json={"decision": "rejected", "reviewer": "li"})
    assert again.status_code == 409

    summary = client.get("/api/recycled-water/summary", params={"usage_date": DAY}).json()
    excluded_keys = {(item["id"], item["status"]) for item in summary["excluded_readings"]}
    assert (out2["id"], "rejected") in excluded_keys
    # confirmed 的负值计入汇总（可复核的回退）：-50 + 120
    east = next(b for b in summary["water_bodies"] if b["water_body"] == "东湖")
    assert east["total_volume_m3"] == 70.0


def test_issued_report_is_immutable_late_data_creates_new_version(client):
    # v1：两个系统上报
    client.post("/api/recycled-water/readings", json=reading(system="RW-1", volume=100, key="v1-a"))
    client.post("/api/recycled-water/readings",
                json=reading(system="RW-2", volume=200, key="v1-b", stage="湿地净化"))
    issued_v1 = client.post("/api/recycled-water/daily-report/issue",
                            json={"water_body": "东湖", "usage_date": DAY, "actor": "clerk-a"})
    assert issued_v1.status_code == 201, issued_v1.text
    v1 = issued_v1.json()
    assert v1["version"] == 1
    assert v1["status"] == "issued"
    assert v1["totals"]["total_volume_m3"] == 300.0

    # 重复签发且输入未变：返回同一版本，不产生新版本
    repeat = client.post("/api/recycled-water/daily-report/issue",
                         json={"water_body": "东湖", "usage_date": DAY, "actor": "clerk-a"})
    assert repeat.json()["version"] == 1
    assert repeat.json()["id"] == v1["id"]

    # 维护期补录：第三个系统迟到
    client.post("/api/recycled-water/readings",
                json=reading(system="RW-3", volume=150, key="late-1", stage="湿地净化"))
    issued_v2 = client.post("/api/recycled-water/daily-report/issue",
                            json={"water_body": "东湖", "usage_date": DAY, "actor": "clerk-a"}).json()
    assert issued_v2["version"] == 2
    assert issued_v2["totals"]["total_volume_m3"] == 450.0

    # v1 内容原封不动，仍可按版本号取回
    fetched_v1 = client.get("/api/recycled-water/daily-report",
                            params={"water_body": "东湖", "usage_date": DAY, "version": 1}).json()
    assert fetched_v1["status"] == "superseded"
    assert fetched_v1["totals"]["total_volume_m3"] == 300.0
    assert fetched_v1["lineage"]["total_volume_m3"] == 300.0

    versions = client.get("/api/recycled-water/daily-report/versions",
                          params={"water_body": "东湖", "usage_date": DAY}).json()["items"]
    assert [v["version"] for v in versions] == [1, 2]
    assert [v["status"] for v in versions] == ["superseded", "issued"]


def test_pending_review_resolution_then_reissue(client):
    client.post("/api/recycled-water/readings", json=reading(volume=100, key="pr-1"))
    flagged = client.post("/api/recycled-water/readings",
                          json=reading(volume=-20, key="pr-2")).json()
    client.post("/api/recycled-water/daily-report/issue",
                json={"water_body": "东湖", "usage_date": DAY, "actor": "a"})

    # 待核查确认后再签发 → 新版本；旧版本不变
    client.post(f"/api/recycled-water/readings/{flagged['id']}/review",
                json={"decision": "confirmed", "reviewer": "wang", "reason": "核实回退"})
    v2 = client.post("/api/recycled-water/daily-report/issue",
                     json={"water_body": "东湖", "usage_date": DAY, "actor": "a"}).json()
    assert v2["version"] == 2
    assert v2["totals"]["total_volume_m3"] == 80.0

    # 汇总中每个环节数字都能指回具体读数
    summary = client.get("/api/recycled-water/summary",
                         params={"usage_date": DAY, "water_body": "东湖"}).json()
    stage = summary["water_bodies"][0]["stages"][0]
    assert stage["process_stage"] == "深度处理"
    assert stage["volume_m3"] == 80.0
    assert len(stage["reading_ids"]) == 2
    assert {s["source_system"] for s in stage["sources"]} == {"RW-1", "RW-1"}


def test_five_systems_multiple_bodies_summary_lineage(client):
    systems = ["RW-1", "RW-2", "RW-3", "RW-4", "RW-5"]
    for idx, sys_code in enumerate(systems):
        client.post("/api/recycled-water/readings", json=reading(
            system=sys_code, body="东湖", stage="深度处理", volume=10 * (idx + 1), key=f"e-{idx}"))
        client.post("/api/recycled-water/readings", json=reading(
            system=sys_code, body="内河", stage="湿地净化", volume=5 * (idx + 1), key=f"r-{idx}"))
    summary = client.get("/api/recycled-water/summary", params={"usage_date": DAY}).json()
    bodies = {b["water_body"]: b for b in summary["water_bodies"]}
    assert bodies["东湖"]["total_volume_m3"] == 150.0  # 10+20+30+40+50
    assert bodies["内河"]["total_volume_m3"] == 75.0
    assert summary["grand_total_m3"] == 225.0
    east = bodies["东湖"]
    assert east["stages"][0]["reading_ids"] == [s["reading_id"] for s in east["stages"][0]["sources"]]
    assert len(east["stages"][0]["sources"]) == 5


def test_state_survives_service_restart(client):
    # 通过 HTTP 建立状态
    client.post("/api/recycled-water/readings", json=reading(volume=120, key="persist-1"))
    flagged = client.post("/api/recycled-water/readings",
                          json=reading(volume=-8, key="persist-2")).json()
    client.post(f"/api/recycled-water/readings/{flagged['id']}/review",
                json={"decision": "rejected", "reviewer": "sun", "reason": "故障数据"})
    client.post("/api/recycled-water/daily-report/issue",
                json={"water_body": "东湖", "usage_date": DAY, "actor": "a"})

    # 模拟服务重启：关闭连接后重新打开同一数据库文件
    close_connection()
    service = RecycledWaterService()
    rows = service.list_readings(usage_date=DAY)
    by_key = {row["reading_key"]: row for row in rows}
    assert by_key["persist-1"]["status"] == "accepted"
    assert by_key["persist-2"]["status"] == "rejected"
    assert by_key["persist-2"]["reviewer"] == "sun"

    report = service.get_report("东湖", DAY)
    assert report["version"] == 1
    assert report["status"] == "issued"
    assert report["totals"]["total_volume_m3"] == 120.0
    pending = service.list_pending()
    assert pending == []
