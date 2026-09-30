"""循环用量核算服务的本地 HTTP 验收脚本。

流程：
1. 在临时 SQLite 文件上启动真实的 uvicorn 服务；
2. 用 HTTP 请求依次验证：重复上报、跨日边界拒绝、异常读数待核查、
   日报签发与不可变、迟到补录只产生新版本、汇总可回溯到读数；
3. 中途重启服务进程，验证版本与核查决定持久化。

用法：
    .venv/bin/python tools/acceptance_recycled_water.py
    .venv/bin/python tools/acceptance_recycled_water.py --port 8499 --keep-db
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-09-29"
NEXT_DAY = "2026-09-30"


class HttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def request(self, method: str, path: str, body: dict | None = None,
                params: dict | None = None) -> tuple[int, dict]:
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def get(self, path: str, **params) -> tuple[int, dict]:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self.request("POST", path, body=body)


def wait_healthy(http: HttpClient) -> None:
    for _ in range(60):
        try:
            status, _ = http.get("/api/system/health")
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionResetError):
            pass
        time.sleep(0.5)
    raise RuntimeError("服务未能在 30 秒内就绪")


def start_server(port: int, db_path: Path) -> subprocess.Popen:
    env = {**os.environ, "TOWNSHIP_DATABASE_PATH": str(db_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def reading(system: str, body: str, stage: str, volume: float, key: str,
            *, day: str = DAY, hour: int = 0) -> dict:
    return {
        "source_system": system,
        "water_body": body,
        "process_stage": stage,
        "period_start": f"{day}T{hour:02d}:00:00+00:00",
        "period_end": f"{day}T{hour:02d}:59:00+00:00",
        "volume_m3": volume,
        "reading_key": key,
    }


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise AssertionError(f"{label} 失败：{detail}")
    print(f"  ✓ {label}")


def run_scenario(http: HttpClient) -> None:
    print("1) 五套系统向东湖、内河上报当日读数")
    systems = ["RW-1", "RW-2", "RW-3", "RW-4", "RW-5"]
    for idx, code in enumerate(systems):
        status, _ = http.post("/api/recycled-water/readings",
                              reading(code, "东湖", "深度处理", 100 + idx * 10, f"east-{idx}"))
        check(f"{code} 东湖上报 201", status == 201, f"status={status}")
        status, _ = http.post("/api/recycled-water/readings",
                              reading(code, "内河", "湿地净化", 50 + idx * 5, f"river-{idx}", hour=1))
        check(f"{code} 内河上报 201", status == 201, f"status={status}")

    print("2) 重复上报幂等，不产生新记录")
    first_status, first = http.post(
        "/api/recycled-water/readings", reading("RW-1", "东湖", "深度处理", 100, "east-0"))
    check("重复请求返回同一条读数", first_status == 201 and first["reading_key"] == "east-0")
    status, items = http.get("/api/recycled-water/readings", usage_date=DAY, water_body="东湖", limit=100)
    check("东湖只有五条读数", len(items["items"]) == 5, json.dumps(items, ensure_ascii=False))

    print("3) 跨日边界读数被拒绝，按自然日拆分后可上报")
    cross = reading("RW-1", "东湖", "深度处理", 40, "cross")
    cross["period_start"] = f"{DAY}T23:30:00+00:00"
    cross["period_end"] = f"{NEXT_DAY}T00:30:00+00:00"
    status, error = http.post("/api/recycled-water/readings", cross)
    check("跨日周期返回 422", status == 422 and "自然日边界" in error["error"]["message"],
          json.dumps(error, ensure_ascii=False))
    part_a = reading("RW-1", "东湖", "深度处理", 25, "cross-a"); part_a["period_start"] = f"{DAY}T23:30:00+00:00"; part_a["period_end"] = f"{DAY}T23:59:59+00:00"
    part_b = reading("RW-1", "东湖", "深度处理", 15, "cross-b", day=NEXT_DAY)
    check("拆分后两段均成功",
          http.post("/api/recycled-water/readings", part_a)[0] == 201
          and http.post("/api/recycled-water/readings", part_b)[0] == 201)

    print("4) 异常读数进入待核查，而不是被排除")
    status, negative = http.post(
        "/api/recycled-water/readings", reading("RW-2", "东湖", "深度处理", -30, "rollback"))
    check("负值读数待核查", status == 201 and negative["status"] == "pending_review",
          json.dumps(negative, ensure_ascii=False))
    status, pending = http.get("/api/recycled-water/readings/pending-review")
    check("待核查清单包含回退读数",
          any(item["id"] == negative["id"] for item in pending["items"]))

    print("5) 签发东湖日报 v1（待核查读数不纳入，但在 pending_reading_ids 中可见）")
    status, v1 = http.post("/api/recycled-water/daily-report/issue",
                           {"water_body": "东湖", "usage_date": DAY, "actor": "duty-clerk"})
    check("v1 签发成功", status == 201 and v1["version"] == 1 and v1["status"] == "issued",
          json.dumps(v1, ensure_ascii=False))
    # 100+110+120+130+140+25(cross-a) = 625
    check("v1 总量为 625（不含待核查）", v1["totals"]["total_volume_m3"] == 625.0,
          json.dumps(v1["totals"], ensure_ascii=False))
    check("v1 标注了待核查读数", v1["lineage"]["pending_reading_ids"] == [negative["id"]])
    stage = next(s for s in v1["lineage"]["stages"] if s["process_stage"] == "深度处理")
    check("v1 数字可指回每条读数",
          sum(s["volume_m3"] for s in stage["sources"]) == stage["volume_m3"] == 625.0)

    print("6) 重复签发不产生新版本")
    status, repeat = http.post("/api/recycled-water/daily-report/issue",
                               {"water_body": "东湖", "usage_date": DAY, "actor": "duty-clerk"})
    check("仍为 v1", repeat["version"] == 1 and repeat["id"] == v1["id"])

    print("7) 运维确认回退读数（维护期补录属实）")
    status, decided = http.post(f"/api/recycled-water/readings/{negative['id']}/review",
                                {"decision": "confirmed", "reviewer": "operator-zhao",
                                 "reason": "维护冲洗回退，读数属实"})
    check("确认成功", status == 200 and decided["status"] == "confirmed",
          json.dumps(decided, ensure_ascii=False))
    status, again = http.post(f"/api/recycled-water/readings/{negative['id']}/review",
                              {"decision": "rejected", "reviewer": "x"})
    check("已处理读数不能再次流转", status == 409, f"status={status}")

    print("8) 确认后重新签发 → v2，v1 原封不动")
    status, v2 = http.post("/api/recycled-water/daily-report/issue",
                           {"water_body": "东湖", "usage_date": DAY, "actor": "duty-clerk"})
    check("生成 v2 且总量含回退（625-30=595）",
          v2["version"] == 2 and v2["totals"]["total_volume_m3"] == 595.0,
          json.dumps(v2.get("totals"), ensure_ascii=False))
    status, fetched_v1 = http.get("/api/recycled-water/daily-report",
                                  water_body="东湖", usage_date=DAY, version=1)
    check("v1 仍可取回且内容不变（300……625）",
          fetched_v1["status"] == "superseded"
          and fetched_v1["totals"]["total_volume_m3"] == 625.0)
    status, versions = http.get("/api/recycled-water/daily-report/versions",
                                water_body="东湖", usage_date=DAY)
    check("版本链为 v1(superseded) → v2(issued)",
          [(v["version"], v["status"]) for v in versions["items"]]
          == [(1, "superseded"), (2, "issued")])

    print("9) 驳回一条异常读数，确认其不进汇总")
    status, bad = http.post(
        "/api/recycled-water/readings", reading("RW-3", "内河", "湿地净化", -999, "faulty"))
    status, rejected = http.post(f"/api/recycled-water/readings/{bad['id']}/review",
                                 {"decision": "rejected", "reviewer": "operator-qian",
                                  "reason": "仪表故障，数据作废"})
    check("驳回成功", rejected["status"] == "rejected")

    print("10) 最终汇总：按水系/环节给出数字与读数来源")
    status, summary = http.get("/api/recycled-water/summary", usage_date=DAY)
    bodies = {b["water_body"]: b for b in summary["water_bodies"]}
    # 东湖：595；内河：50+55+60+65+70 = 300（驳回的 -999 不纳入）
    check("东湖合计 595", bodies["东湖"]["total_volume_m3"] == 595.0,
          json.dumps(bodies["东湖"], ensure_ascii=False))
    check("内河合计 300", bodies["内河"]["total_volume_m3"] == 300.0)
    check("总合计 895", summary["grand_total_m3"] == 895.0)
    excluded = {(e["id"], e["status"]) for e in summary["excluded_readings"]}
    check("驳回读数在 excluded_readings 中可查",
          (bad["id"], "rejected") in excluded and not any(s == "pending_review" for _, s in excluded))
    river_stage = bodies["内河"]["stages"][0]
    check("内河数字逐条可回溯",
          [s["source_system"] for s in river_stage["sources"]] == systems
          and river_stage["reading_ids"] == [s["reading_id"] for s in river_stage["sources"]])

    print("11) 跨日另一半读数落在次日")
    status, next_day = http.get("/api/recycled-water/summary", usage_date=NEXT_DAY)
    check("次日汇总只含 cross-b 的 15", next_day["grand_total_m3"] == 15.0,
          json.dumps(next_day, ensure_ascii=False))
    return {"v1_id": v1["id"], "negative_id": negative["id"], "bad_id": bad["id"]}


def verify_after_restart(http: HttpClient, ids: dict) -> None:
    print("12) 服务重启后校验持久化")
    status, report = http.get("/api/recycled-water/daily-report",
                              water_body="东湖", usage_date=DAY)
    check("最新版本仍为 v2(issued)",
          report["version"] == 2 and report["status"] == "issued"
          and report["totals"]["total_volume_m3"] == 595.0)
    status, v1 = http.get("/api/recycled-water/daily-report",
                          water_body="东湖", usage_date=DAY, version=1)
    check("已签发 v1 未被改写",
          v1["status"] == "superseded" and v1["totals"]["total_volume_m3"] == 625.0
          and v1["id"] == ids["v1_id"])
    status, neg = http.get(f"/api/recycled-water/readings/{ids['negative_id']}")
    check("确认决定保留", status == 200 and neg.get("status") == "confirmed"
          and neg.get("reviewer") == "operator-zhao", json.dumps(neg, ensure_ascii=False))
    status, bad = http.get(f"/api/recycled-water/readings/{ids['bad_id']}")
    check("驳回决定保留", bad["status"] == "rejected" and bad["reviewer"] == "operator-qian")
    status, pending = http.get("/api/recycled-water/readings/pending-review")
    check("待核查队列为空", pending["items"] == [])


def main() -> int:
    parser = argparse.ArgumentParser(description="循环用量核算服务 HTTP 验收")
    parser.add_argument("--port", type=int, default=8499)
    parser.add_argument("--keep-db", action="store_true", help="保留临时数据库文件")
    args = parser.parse_args()

    tmp_dir = Path(tempfile.mkdtemp(prefix="recycled-acceptance-"))
    db_path = tmp_dir / "acceptance.db"
    print(f"数据库：{db_path}")
    proc = start_server(args.port, db_path)
    http = HttpClient(f"http://127.0.0.1:{args.port}")
    try:
        wait_healthy(http)
        print(f"服务已启动（端口 {args.port}）\n")
        ids = run_scenario(http)

        print("\n-- 重启服务进程 --")
        stop_server(proc)
        proc = start_server(args.port, db_path)
        wait_healthy(http)
        verify_after_restart(http, ids)
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 验收失败：{exc}", file=sys.stderr)
        return 1
    finally:
        stop_server(proc)
        if args.keep_db:
            print(f"\n数据库已保留：{db_path}")
        else:
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            tmp_dir.rmdir()
    print("\n全部验收通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
