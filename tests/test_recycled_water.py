from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


def register_source(client, code: str = "RW-01", **overrides) -> None:
    payload = {"code": code, "name": f"再生水系统 {code}", "max_volume_m3": 5000, "max_period_hours": 48, **overrides}
    response = client.put("/api/recycled-water/sources", json=payload)
    assert response.status_code == 200, response.text


def reading_payload(client_ref: str, **overrides) -> dict:
    payload = {
        "source_code": "RW-01",
        "water_body": "北湖",
        "stage": "生态补水",
        "period_start": "2026-09-28T08:00:00+08:00",
        "period_end": "2026-09-28T10:00:00+08:00",
        "volume_m3": 120.0,
        "client_ref": client_ref,
        "submitted_by": "plant-1",
    }
    payload.update(overrides)
    return payload


def submit(client, client_ref: str, **overrides):
    response = client.post("/api/recycled-water/readings", json=reading_payload(client_ref, **overrides))
    assert response.status_code in (200, 201), response.text
    return response.json()


# ------------------------------------------------------------ 重复上报


def test_duplicate_submission_is_idempotent(client):
    register_source(client)
    first = client.post("/api/recycled-water/readings", json=reading_payload("dup-001"))
    second = client.post("/api/recycled-water/readings", json=reading_payload("dup-001"))
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["deduped"] is True
    assert second.json()["id"] == first.json()["id"]
    items = client.get("/api/recycled-water/readings").json()["items"]
    assert len(items) == 1


def test_same_client_ref_with_different_payload_is_conflict(client):
    register_source(client)
    first = client.post("/api/recycled-water/readings", json=reading_payload("dup-002", volume_m3=100))
    assert first.status_code == 201
    changed = reading_payload("dup-002", volume_m3=101)
    response = client.post("/api/recycled-water/readings", json=changed)
    assert response.status_code == 409
    assert response.json()["error"]["context"]["existing_reading_id"] == first.json()["id"]


def test_naive_datetime_and_non_finite_volume_rejected(client):
    register_source(client)
    bad_tz = reading_payload("bad-tz")
    bad_tz["period_start"] = "2026-09-28T08:00:00"
    assert client.post("/api/recycled-water/readings", json=bad_tz).status_code == 422
    bad_order = reading_payload("bad-order", period_start="2026-09-28T10:00:00+08:00", period_end="2026-09-28T08:00:00+08:00")
    assert client.post("/api/recycled-water/readings", json=bad_order).status_code == 422
    bad_volume = reading_payload("bad-vol", volume_m3="NaN")
    assert client.post("/api/recycled-water/readings", json=bad_volume).status_code == 422


# ------------------------------------------------------------ 跨日边界


def test_cross_midnight_reading_is_split_by_natural_day(client):
    register_source(client)
    # 20:00 到次日 04:00，共 8 小时，800 立方米，按秒均摊为各 400。
    result = submit(
        client,
        "cross-001",
        period_start="2026-09-28T20:00:00+08:00",
        period_end="2026-09-29T04:00:00+08:00",
        volume_m3=800,
    )
    assert result["allocations"] == [
        {"day": "2026-09-28", "volume_m3": 400.0},
        {"day": "2026-09-29", "volume_m3": 400.0},
    ]
    day1 = client.get("/api/recycled-water/reports/2026-09-28").json()
    day2 = client.get("/api/recycled-water/reports/2026-09-29").json()
    assert day1["total_volume_m3"] == 400.0
    assert day2["total_volume_m3"] == 400.0


def test_timezone_offset_honored_for_day_boundary(client):
    register_source(client)
    # UTC 时间 2026-09-28T16:30Z 对应北京时间 2026-09-29 00:30，落在 29 日。
    result = submit(
        client,
        "tz-001",
        period_start="2026-09-28T16:30:00+00:00",
        period_end="2026-09-28T17:30:00+00:00",
        volume_m3=60,
    )
    assert result["allocations"] == [{"day": "2026-09-29", "volume_m3": 60.0}]


def test_multi_system_water_body_and_stage_aggregation(client):
    register_source(client, "RW-01")
    register_source(client, "RW-02")
    submit(client, "m-1", source_code="RW-01", water_body="北湖", stage="深度处理出水", volume_m3=100)
    submit(client, "m-2", source_code="RW-02", water_body="北湖", stage="深度处理出水", volume_m3=50)
    submit(client, "m-3", source_code="RW-01", water_body="内河", stage="生态补水", volume_m3=30)
    report = client.get("/api/recycled-water/reports/2026-09-28").json()
    totals = {(line["water_body"], line["stage"]): line["volume_m3"] for line in report["lines"]}
    assert totals == {("北湖", "深度处理出水"): 150.0, ("内河", "生态补水"): 30.0}


# ------------------------------------------------------------ 异常核查流转


def test_abnormal_reading_enters_flagged_and_is_not_silently_dropped(client):
    register_source(client)
    normal = submit(client, "flag-1", volume_m3=100)
    assert normal["status"] == "accepted"
    abnormal = submit(client, "flag-2", volume_m3=999999)
    assert abnormal["status"] == "flagged"
    assert "above_source_limit" in abnormal["flag_reason"]
    negative = submit(client, "flag-3", volume_m3=-5)
    assert negative["status"] == "flagged"
    report = client.get("/api/recycled-water/reports/2026-09-28").json()
    # 待核查读数不进入合计，但明确显示有待核查项。
    assert report["total_volume_m3"] == 100.0
    assert report["pending_flagged_readings"] == 2
    flagged_items = client.get("/api/recycled-water/readings", params={"status": "flagged"}).json()["items"]
    assert {item["id"] for item in flagged_items} == {abnormal["id"], negative["id"]}


def test_review_accept_and_reject_flow(client):
    register_source(client)
    flagged = submit(client, "rev-1", volume_m3=999999)
    reading_id = flagged["id"]
    # 非待核查状态不能审核。
    accepted = submit(client, "rev-2", volume_m3=10)
    conflict = client.post(f"/api/recycled-water/readings/{accepted['id']}/review", json={"decision": "accepted", "reviewer": "r"})
    assert conflict.status_code == 409

    rejected = client.post(
        f"/api/recycled-water/readings/{reading_id}/review",
        json={"decision": "rejected", "reviewer": "审核员甲", "note": "明显超量程"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["reviewer"] == "审核员甲"
    report = client.get("/api/recycled-water/reports/2026-09-28").json()
    assert report["total_volume_m3"] == 10.0
    # 驳回后可以重新打开核查。
    reopened = client.post(f"/api/recycled-water/readings/{reading_id}/reopen", json={"operator": "审核员乙"})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "flagged"
    accepted_now = client.post(
        f"/api/recycled-water/readings/{reading_id}/review",
        json={"decision": "accepted", "reviewer": "审核员乙", "note": "现场复核表计正常"},
    )
    assert accepted_now.json()["status"] == "accepted"
    detail = client.get(f"/api/recycled-water/readings/{reading_id}").json()
    actions = [event["action"] for event in detail["events"]]
    assert actions == ["received", "review_rejected", "reopened", "review_accepted"]


def test_withdraw_and_reinstate_during_maintenance(client):
    register_source(client)
    reading = submit(client, "wd-1", volume_m3=100)
    withdrawn = client.post(
        f"/api/recycled-water/readings/{reading['id']}/withdraw",
        json={"reason": "维护期表计故障", "operator": "运维工"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    report = client.get("/api/recycled-water/reports/2026-09-28").json()
    assert report["total_volume_m3"] == 0.0
    reinstated = client.post(
        f"/api/recycled-water/readings/{reading['id']}/reinstate",
        json={"reason": "表计恢复，补录有效", "operator": "运维工"},
    )
    assert reinstated.json()["status"] == "accepted"
    assert client.get("/api/recycled-water/reports/2026-09-28").json()["total_volume_m3"] == 100.0


# ------------------------------------------------------------ 版本化日报


def test_issued_report_is_immutable_and_late_data_creates_new_version(client):
    register_source(client)
    submit(client, "v-1", volume_m3=100)
    issued = client.post("/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管"})
    assert issued.status_code == 200
    v1 = issued.json()
    assert v1["version"] == 1
    assert v1["status"] == "issued"
    assert v1["total_volume_m3"] == 100.0

    # 迟到补录一条读数。
    submit(client, "v-2-late", volume_m3=40)
    current = client.get("/api/recycled-water/reports/2026-09-28").json()
    assert current["version"] == 1
    assert current["outdated"] is True

    diff = client.get("/api/recycled-water/reports/2026-09-28/diff").json()
    assert diff["changed"] is True
    assert diff["line_changes"] == [
        {"water_body": "北湖", "stage": "生态补水", "previous_volume_m3": 100.0, "current_volume_m3": 140.0}
    ]

    # 不带 confirm 必须拒绝，防止悄悄改写已签发日报。
    blocked = client.post("/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管"})
    assert blocked.status_code == 409
    # 已签发版本仍然保持原值。
    frozen = client.get("/api/recycled-water/reports/2026-09-28/versions/1").json()
    assert frozen["total_volume_m3"] == 100.0
    assert frozen["status"] == "issued"

    issued2 = client.post(
        "/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管", "confirm": True}
    )
    assert issued2.status_code == 200
    assert issued2.json()["version"] == 2
    assert issued2.json()["total_volume_m3"] == 140.0

    versions = client.get("/api/recycled-water/reports/2026-09-28/versions").json()["items"]
    assert [item["version"] for item in versions] == [1, 2]
    assert all(item["status"] == "issued" for item in versions)
    # 内容摘要不同，可证版本快照未被覆盖。
    assert versions[0]["content_digest"] != versions[1]["content_digest"]


def test_issue_without_changes_is_noop_conflict(client):
    register_source(client)
    submit(client, "nc-1", volume_m3=100)
    assert client.post("/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管"}).status_code == 200
    again = client.post(
        "/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管", "confirm": True}
    )
    assert again.status_code == 409


def test_review_decision_after_issue_creates_traceable_new_version(client):
    register_source(client)
    submit(client, "rv-1", volume_m3=100)
    flagged = submit(client, "rv-2", volume_m3=6000)
    client.post("/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管"})
    # 待核查读数被快照进 v1 但不计入合计。
    v1 = client.get("/api/recycled-water/reports/2026-09-28/versions/1").json()
    assert v1["total_volume_m3"] == 100.0
    snapshot = {component["reading_id"]: component["reading_status"] for component in v1["components"]}
    assert snapshot[flagged["id"]] == "flagged"

    client.post(
        f"/api/recycled-water/readings/{flagged['id']}/review",
        json={"decision": "accepted", "reviewer": "审核员", "note": "核查通过"},
    )
    diff = client.get("/api/recycled-water/reports/2026-09-28/diff").json()
    assert diff["reading_status_changes"] == [
        {"reading_id": flagged["id"], "previous_status": "flagged", "current_status": "accepted"}
    ]
    v2 = client.post(
        "/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管", "confirm": True}
    ).json()
    assert v2["total_volume_m3"] == 6100.0
    line = v2["lines"][0]
    assert (line["water_body"], line["stage"], line["volume_m3"]) == ("北湖", "生态补水", 6100.0)
    # v1 中该读数仍留档为 flagged。
    frozen = client.get("/api/recycled-water/reports/2026-09-28/versions/1").json()
    frozen_status = {c["reading_id"]: c["reading_status"] for c in frozen["components"]}
    assert frozen_status[flagged["id"]] == "flagged"


# ------------------------------------------------------------ 汇总溯源


def test_summary_points_to_source_readings_for_each_number(client):
    register_source(client, "RW-01")
    register_source(client, "RW-02")
    first = submit(client, "s-1", source_code="RW-01", water_body="北湖", stage="深度处理出水", volume_m3=100)
    second = submit(client, "s-2", source_code="RW-02", water_body="北湖", stage="深度处理出水", volume_m3=50)
    submit(
        client,
        "s-3",
        source_code="RW-01",
        water_body="北湖",
        stage="深度处理出水",
        volume_m3=800,
        period_start="2026-09-28T20:00:00+08:00",
        period_end="2026-09-29T04:00:00+08:00",
    )
    client.post("/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管"})
    summary = client.get("/api/recycled-water/summary", params={"from": "2026-09-28", "to": "2026-09-29"}).json()
    assert summary["grand_total_m3"] == 550.0
    assert summary["total_by_water_body_m3"] == {"北湖": 550.0}
    day28 = next(day for day in summary["days"] if day["report_day"] == "2026-09-28")
    line = day28["lines"][0]
    assert line["volume_m3"] == 550.0
    source_ids = {item["reading_id"] for item in line["sources"]}
    assert source_ids == {first["id"], second["id"], 3}
    contributions = {item["reading_id"]: item["signed_volume_m3"] for item in line["sources"]}
    assert contributions == {first["id"]: 100.0, second["id"]: 50.0, 3: 400.0}


# ------------------------------------------------------------ 重启持久化


def test_state_survives_service_restart(client):
    from app.database import close_connection

    register_source(client)
    flagged = submit(client, "persist-1", volume_m3=7000)
    submit(client, "persist-2", volume_m3=120)
    client.post(
        f"/api/recycled-water/readings/{flagged['id']}/review",
        json={"decision": "rejected", "reviewer": "审核员", "note": "驳回留档"},
    )
    client.post("/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管"})
    db_path = os.environ["TOWNSHIP_DATABASE_PATH"]
    assert Path(db_path).exists()

    # 模拟服务重启：关闭连接后用同一数据库新建客户端，lifespan 重新建表（幂等）。
    close_connection()
    from app.main import app

    with TestClient(app) as restarted:
        reading = restarted.get(f"/api/recycled-water/readings/{flagged['id']}").json()
        assert reading["status"] == "rejected"
        assert reading["reviewer"] == "审核员"
        versions = restarted.get("/api/recycled-water/reports/2026-09-28/versions").json()["items"]
        assert len(versions) == 1 and versions[0]["status"] == "issued"
        frozen = restarted.get("/api/recycled-water/reports/2026-09-28/versions/1").json()
        assert frozen["total_volume_m3"] == 120.0
        # 迟到数据在重启后仍只能形成新版本。
        submit_via = restarted.post(
            "/api/recycled-water/readings",
            json=reading_payload("persist-3", volume_m3=30),
        )
        assert submit_via.status_code == 201
        blocked = restarted.post(
            "/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管"}
        )
        assert blocked.status_code == 409
        v2 = restarted.post(
            "/api/recycled-water/reports/2026-09-28/issue", json={"actor": "主管", "confirm": True}
        ).json()
        assert v2["version"] == 2 and v2["total_volume_m3"] == 150.0
