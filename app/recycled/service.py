from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

# ---------------------------------------------------------------------------
# 存储结构
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS recycled_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,
    water_body TEXT NOT NULL,
    process_stage TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    volume_m3 REAL NOT NULL,
    reading_key TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK(status IN ('accepted','pending_review','confirmed','rejected')),
    flagged_reason TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewer TEXT NOT NULL DEFAULT '',
    review_reason TEXT NOT NULL DEFAULT '',
    supersedes_reading_id INTEGER REFERENCES recycled_readings(id),
    UNIQUE(source_system, reading_key)
);
CREATE INDEX IF NOT EXISTS idx_recycled_readings_day
    ON recycled_readings(usage_date, water_body, process_stage, status);
CREATE INDEX IF NOT EXISTS idx_recycled_readings_status ON recycled_readings(status);

CREATE TABLE IF NOT EXISTS recycled_report_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    water_body TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','issued','superseded')),
    totals_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    issued_at TEXT,
    issued_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(water_body, usage_date, version)
);
CREATE INDEX IF NOT EXISTS idx_recycled_reports_lookup
    ON recycled_report_versions(water_body, usage_date, status);

CREATE TABLE IF NOT EXISTS recycled_report_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_version_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

# 视为异常、需要人工核查的读数规则
NEGATIVE_REASON = "水量为负，疑似回退或抄表错误"
OUTLIER_REASON = "同系统同环节同自然日水量偏离中位值 3 倍以上"
OUTLIER_MIN_PEERS = 2  # 至少有这么多条对照读数才做离群判断


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _parse_ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法的 ISO8601 时间：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _day_bounds(usage_date: str) -> tuple[datetime, datetime]:
    try:
        day = datetime.strptime(usage_date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValidationError(f"usage_date 不是合法的自然日：{usage_date}") from exc
    return day, day.replace(hour=23, minute=59, second=59, microsecond=999999)


def _round2(value: float) -> float:
    return round(value + 0.0, 2)


class RecycledWaterService:
    """接收读数并生成按水系、处理环节、自然日可复核的用量链。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------
    # 读数上报
    # ------------------------------------------------------------------

    def ingest_reading(self, payload: dict[str, Any]) -> dict[str, Any]:
        start = _parse_ts(payload["period_start"], "period_start")
        end = _parse_ts(payload["period_end"], "period_end")
        if end <= start:
            raise ValidationError("计量周期结束时间必须晚于开始时间")
        if payload["volume_m3"] != payload["volume_m3"]:  # NaN
            raise ValidationError("水量不能为 NaN")
        if payload["volume_m3"] in (float("inf"), float("-inf")):
            raise ValidationError("水量必须为有限数值")

        usage_date = start.date().isoformat()
        if end.date().isoformat() != usage_date:
            raise ValidationError(
                "计量周期跨越了自然日边界，请按自然日拆分后再上报",
                context={"period_start": payload["period_start"], "period_end": payload["period_end"]},
            )

        received = to_storage(
            _parse_ts(payload["reported_at"], "reported_at") if payload.get("reported_at") else self.clock.now()
        )
        reading_key = payload.get("reading_key") or self._derive_key(payload, start, end)

        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM recycled_readings WHERE source_system=? AND reading_key=?",
                (payload["source_system"], reading_key),
            ).fetchone()
            if existing is not None:
                # 重复上报：原样返回，不产生新记录、不改变任何状态
                return dict(existing)

            cursor = connection.execute(
                """INSERT INTO recycled_readings(
                       source_system, water_body, process_stage, period_start, period_end,
                       usage_date, volume_m3, reading_key, note, status, received_at)
                   VALUES(?,?,?,?,?,?,?,?,?, 'accepted', ?)""",
                (
                    payload["source_system"], payload["water_body"], payload["process_stage"],
                    to_storage(start), to_storage(end), usage_date, float(payload["volume_m3"]),
                    reading_key, payload.get("note", ""), received,
                ),
            )
            reading_id = cursor.lastrowid
            self._flag_if_abnormal(connection, reading_id)
            row = connection.execute("SELECT * FROM recycled_readings WHERE id=?", (reading_id,)).fetchone()
            return dict(row)

    @staticmethod
    def _derive_key(payload: dict[str, Any], start: datetime, end: datetime) -> str:
        basis = {
            "source_system": payload["source_system"],
            "water_body": payload["water_body"],
            "process_stage": payload["process_stage"],
            "period_start": to_storage(start),
            "period_end": to_storage(end),
            "volume_m3": round(float(payload["volume_m3"]), 6),
        }
        return "auto-" + _digest(basis)[:24]

    def _flag_if_abnormal(self, connection: sqlite3.Connection, reading_id: int) -> None:
        row = connection.execute("SELECT * FROM recycled_readings WHERE id=?", (reading_id,)).fetchone()
        reasons: list[str] = []
        if float(row["volume_m3"]) < 0:
            reasons.append(NEGATIVE_REASON)
        peers = connection.execute(
            """SELECT volume_m3 FROM recycled_readings
               WHERE source_system=? AND process_stage=? AND usage_date=? AND id<>?
                 AND status IN ('accepted','confirmed')
               ORDER BY volume_m3""",
            (row["source_system"], row["process_stage"], row["usage_date"], reading_id),
        ).fetchall()
        if len(peers) >= OUTLIER_MIN_PEERS:
            values = sorted(float(item["volume_m3"]) for item in peers)
            middle = len(values) // 2
            median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
            if median > 0 and abs(float(row["volume_m3"]) - median) > 3 * median:
                reasons.append(OUTLIER_REASON)
        if reasons:
            connection.execute(
                "UPDATE recycled_readings SET status='pending_review', flagged_reason=? WHERE id=?",
                ("；".join(reasons), reading_id),
            )

    # ------------------------------------------------------------------
    # 核查流转
    # ------------------------------------------------------------------

    def list_readings(self, *, usage_date: str | None = None, water_body: str | None = None,
                      status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if usage_date:
            where.append("usage_date=?")
            params.append(usage_date)
        if water_body:
            where.append("water_body=?")
            params.append(water_body)
        if status:
            where.append("status=?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.connection.execute(
            f"SELECT * FROM recycled_readings{clause} ORDER BY usage_date, water_body, process_stage, id "
            f"LIMIT {max(1, min(limit, 1000))}",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_reading(self, reading_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM recycled_readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def list_pending(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM recycled_readings WHERE status='pending_review' ORDER BY received_at, id"
        ).fetchall()
        return [dict(row) for row in rows]

    def review_reading(self, reading_id: int, decision: str, reviewer: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM recycled_readings WHERE id=?", (reading_id,)).fetchone()
            if row is None:
                raise NotFoundError("读数不存在")
            if row["status"] != "pending_review":
                raise ConflictError(
                    "只有待核查读数可以确认或驳回",
                    context={"reading_id": reading_id, "current_status": row["status"]},
                )
            new_status = "confirmed" if decision == "confirmed" else "rejected"
            connection.execute(
                """UPDATE recycled_readings
                   SET status=?, reviewed_at=?, reviewer=?, review_reason=? WHERE id=?""",
                (new_status, now, reviewer, reason, reading_id),
            )
            after = dict(connection.execute("SELECT * FROM recycled_readings WHERE id=?", (reading_id,)).fetchone())
            connection.execute(
                "INSERT INTO recycled_report_audit(report_version_id,action,actor,detail_json,created_at) VALUES(NULL,?,?,?,?)",
                (f"reading.{decision}", reviewer,
                 json.dumps({"reading_id": reading_id, "reason": reason, "before_status": "pending_review",
                             "after_status": new_status}, ensure_ascii=False), now),
            )
            return after

    # ------------------------------------------------------------------
    # 日报版本
    # ------------------------------------------------------------------

    def build_daily_report(self, water_body: str, usage_date: str) -> dict[str, Any]:
        """按当前读数计算一版日报内容（不落库）。"""
        rows = self.connection.execute(
            """SELECT * FROM recycled_readings
               WHERE water_body=? AND usage_date=? AND status IN ('accepted','confirmed')
               ORDER BY process_stage, source_system, id""",
            (water_body, usage_date),
        ).fetchall()
        stages: dict[str, dict[str, Any]] = {}
        total = 0.0
        for row in rows:
            stage = stages.setdefault(
                row["process_stage"],
                {"process_stage": row["process_stage"], "volume_m3": 0.0, "reading_ids": [], "sources": []},
            )
            stage["volume_m3"] = _round2(stage["volume_m3"] + float(row["volume_m3"]))
            stage["reading_ids"].append(row["id"])
            source_entry = {"source_system": row["source_system"], "reading_id": row["id"],
                            "volume_m3": _round2(float(row["volume_m3"])), "status": row["status"]}
            stage["sources"].append(source_entry)
            total += float(row["volume_m3"])
        pending = [dict(row) for row in self.connection.execute(
            "SELECT id FROM recycled_readings WHERE water_body=? AND usage_date=? AND status='pending_review'",
            (water_body, usage_date),
        ).fetchall()]
        return {
            "water_body": water_body,
            "usage_date": usage_date,
            "total_volume_m3": _round2(total),
            "stages": [stages[key] for key in sorted(stages)],
            "pending_reading_ids": [item["id"] for item in pending],
        }

    def issue_daily_report(self, water_body: str, usage_date: str, actor: str) -> dict[str, Any]:
        """签发或追加日报版本。迟到数据只产生新版本，已签发版本永不改写。"""
        _day_bounds(usage_date)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            content = self._build_report_on(connection, water_body, usage_date)
            digest = _digest({"totals": content["stages"], "total": content["total_volume_m3"],
                              "readings": self._reading_signatures(connection, water_body, usage_date)})

            latest = connection.execute(
                """SELECT * FROM recycled_report_versions
                   WHERE water_body=? AND usage_date=? ORDER BY version DESC LIMIT 1""",
                (water_body, usage_date),
            ).fetchone()

            if latest is not None and latest["status"] == "issued" and latest["input_digest"] == digest:
                # 输入未变：重复签发直接返回已签发版本
                return self._report_row(connection, latest)

            if latest is not None and latest["status"] == "issued":
                connection.execute("UPDATE recycled_report_versions SET status='superseded' WHERE id=?",
                                   (latest["id"],))
            version = int(latest["version"]) + 1 if latest is not None else 1
            cursor = connection.execute(
                """INSERT INTO recycled_report_versions(
                       water_body, usage_date, version, status, totals_json, lineage_json,
                       input_digest, issued_at, issued_by, created_at)
                   VALUES(?,?,?, 'issued', ?, ?, ?, ?, ?, ?)""",
                (water_body, usage_date, version,
                 json.dumps({"total_volume_m3": content["total_volume_m3"], "stages": content["stages"]},
                            ensure_ascii=False, sort_keys=True),
                 json.dumps(content, ensure_ascii=False, sort_keys=True),
                 digest, now, actor, now),
            )
            report_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO recycled_report_audit(report_version_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, "report.issue", actor,
                 json.dumps({"version": version, "supersedes": latest["id"] if latest else None,
                             "total_volume_m3": content["total_volume_m3"],
                             "pending_reading_ids": content["pending_reading_ids"]}, ensure_ascii=False), now),
            )
            return self._report_row(connection,
                                    connection.execute("SELECT * FROM recycled_report_versions WHERE id=?",
                                                       (report_id,)).fetchone())

    @staticmethod
    def _build_report_on(connection: sqlite3.Connection, water_body: str, usage_date: str) -> dict[str, Any]:
        rows = connection.execute(
            """SELECT * FROM recycled_readings
               WHERE water_body=? AND usage_date=? AND status IN ('accepted','confirmed')
               ORDER BY process_stage, source_system, id""",
            (water_body, usage_date),
        ).fetchall()
        stages: dict[str, dict[str, Any]] = {}
        total = 0.0
        for row in rows:
            stage = stages.setdefault(
                row["process_stage"],
                {"process_stage": row["process_stage"], "volume_m3": 0.0, "reading_ids": [], "sources": []},
            )
            stage["volume_m3"] = _round2(stage["volume_m3"] + float(row["volume_m3"]))
            stage["reading_ids"].append(row["id"])
            stage["sources"].append({
                "source_system": row["source_system"], "reading_id": row["id"],
                "volume_m3": _round2(float(row["volume_m3"])), "status": row["status"],
            })
            total += float(row["volume_m3"])
        pending = connection.execute(
            "SELECT id FROM recycled_readings WHERE water_body=? AND usage_date=? AND status='pending_review' ORDER BY id",
            (water_body, usage_date),
        ).fetchall()
        return {
            "water_body": water_body,
            "usage_date": usage_date,
            "total_volume_m3": _round2(total),
            "stages": [stages[key] for key in sorted(stages)],
            "pending_reading_ids": [item["id"] for item in pending],
        }

    @staticmethod
    def _reading_signatures(connection: sqlite3.Connection, water_body: str, usage_date: str) -> list[list[Any]]:
        rows = connection.execute(
            """SELECT id, status, volume_m3 FROM recycled_readings
               WHERE water_body=? AND usage_date=? ORDER BY id""",
            (water_body, usage_date),
        ).fetchall()
        return [[row["id"], row["status"], _round2(float(row["volume_m3"]))] for row in rows]

    @staticmethod
    def _report_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["totals"] = json.loads(result.pop("totals_json"))
        result["lineage"] = json.loads(result.pop("lineage_json"))
        return result

    def get_report(self, water_body: str, usage_date: str, version: int | None = None) -> dict[str, Any]:
        if version is None:
            row = self.connection.execute(
                """SELECT * FROM recycled_report_versions
                   WHERE water_body=? AND usage_date=? ORDER BY version DESC LIMIT 1""",
                (water_body, usage_date),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM recycled_report_versions WHERE water_body=? AND usage_date=? AND version=?",
                (water_body, usage_date, version),
            ).fetchone()
        if row is None:
            raise NotFoundError("日报版本不存在")
        result = self._report_row(self.connection, row)
        result["history"] = [
            {"version": item["version"], "status": item["status"], "issued_at": item["issued_at"],
             "issued_by": item["issued_by"],
             "total_volume_m3": json.loads(item["totals_json"])["total_volume_m3"]}
            for item in self.connection.execute(
                "SELECT version,status,issued_at,issued_by,totals_json FROM recycled_report_versions "
                "WHERE water_body=? AND usage_date=? ORDER BY version",
                (water_body, usage_date),
            ).fetchall()
        ]
        return result

    def list_report_versions(self, water_body: str, usage_date: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT version,status,issued_at,issued_by,totals_json,input_digest,created_at "
            "FROM recycled_report_versions WHERE water_body=? AND usage_date=? ORDER BY version",
            (water_body, usage_date),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["total_volume_m3"] = json.loads(item.pop("totals_json"))["total_volume_m3"]
            items.append(item)
        return items

    # ------------------------------------------------------------------
    # 最终汇总
    # ------------------------------------------------------------------

    def summarize(self, usage_date: str, water_body: str | None = None) -> dict[str, Any]:
        """每个数字都指出由哪些读数构成；只汇总已纳入（accepted/confirmed）的读数。"""
        _day_bounds(usage_date)
        query = (
            "SELECT water_body, process_stage, id, source_system, volume_m3, status "
            "FROM recycled_readings WHERE usage_date=? AND status IN ('accepted','confirmed')"
        )
        params: list[Any] = [usage_date]
        if water_body:
            query += " AND water_body=?"
            params.append(water_body)
        query += " ORDER BY water_body, process_stage, source_system, id"
        rows = self.connection.execute(query, params).fetchall()

        bodies: dict[str, dict[str, Any]] = {}
        for row in rows:
            body = bodies.setdefault(
                row["water_body"],
                {"water_body": row["water_body"], "total_volume_m3": 0.0,
                 "stage_totals": {}, "stages": []},
            )
            stage = body["stage_totals"].setdefault(
                row["process_stage"],
                {"process_stage": row["process_stage"], "volume_m3": 0.0, "reading_ids": [], "sources": []},
            )
            stage["volume_m3"] = _round2(stage["volume_m3"] + float(row["volume_m3"]))
            stage["reading_ids"].append(row["id"])
            stage["sources"].append({"source_system": row["source_system"], "reading_id": row["id"],
                                     "volume_m3": _round2(float(row["volume_m3"])), "status": row["status"]})
            body["total_volume_m3"] += float(row["volume_m3"])

        result_bodies = []
        grand_total = 0.0
        for body_name in sorted(bodies):
            body = bodies[body_name]
            body["stages"] = [body["stage_totals"][key] for key in sorted(body["stage_totals"])]
            del body["stage_totals"]
            body["total_volume_m3"] = _round2(body["total_volume_m3"])
            grand_total += body["total_volume_m3"]
            result_bodies.append(body)

        excluded_query = (
            "SELECT id, source_system, water_body, process_stage, volume_m3, status, flagged_reason "
            "FROM recycled_readings WHERE usage_date=?"
            + (" AND water_body=?" if water_body else "")
            + " AND status IN ('pending_review','rejected') ORDER BY id"
        )
        excluded = [dict(row) for row in self.connection.execute(excluded_query, params).fetchall()]
        return {
            "usage_date": usage_date,
            "water_body": water_body,
            "grand_total_m3": _round2(grand_total),
            "water_bodies": result_bodies,
            "excluded_readings": excluded,
        }
