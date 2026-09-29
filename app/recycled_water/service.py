from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

# 公园台账按北京时间划分自然日，跨日读数以此为界分摊。
REPORT_TZ = timezone(timedelta(hours=8))
VOLUME_SCALE = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS rw_sources (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    max_volume_m3 REAL,
    max_period_hours REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rw_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT NOT NULL,
    water_body TEXT NOT NULL,
    stage TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    volume_m3 REAL NOT NULL,
    client_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('accepted','flagged','rejected','withdrawn')),
    flag_reason TEXT NOT NULL DEFAULT '',
    submitted_by TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    is_late INTEGER NOT NULL DEFAULT 0 CHECK(is_late IN (0,1)),
    received_at TEXT NOT NULL,
    reviewed_at TEXT,
    withdrawn_at TEXT,
    withdrawn_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rw_readings_period ON rw_readings(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_rw_readings_status ON rw_readings(status);
CREATE TABLE IF NOT EXISTS rw_reading_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES rw_readings(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rw_reading_events_reading ON rw_reading_events(reading_id, id);
CREATE TABLE IF NOT EXISTS rw_report_keys (
    report_day TEXT PRIMARY KEY,
    latest_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rw_report_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_day TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('issued','voided')),
    issued_by TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    voided_by TEXT NOT NULL DEFAULT '',
    voided_at TEXT,
    void_reason TEXT NOT NULL DEFAULT '',
    total_volume_m3 REAL NOT NULL,
    content_digest TEXT NOT NULL,
    UNIQUE(report_day, version)
);
CREATE TABLE IF NOT EXISTS rw_report_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_day TEXT NOT NULL,
    version INTEGER NOT NULL,
    water_body TEXT NOT NULL,
    stage TEXT NOT NULL,
    volume_m3 REAL NOT NULL,
    UNIQUE(report_day, version, water_body, stage)
);
CREATE TABLE IF NOT EXISTS rw_report_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_day TEXT NOT NULL,
    version INTEGER NOT NULL,
    line_water_body TEXT NOT NULL,
    line_stage TEXT NOT NULL,
    reading_id INTEGER NOT NULL,
    source_code TEXT NOT NULL,
    reading_status TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    reading_volume_m3 REAL NOT NULL,
    signed_volume_m3 REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rw_components_version ON rw_report_components(report_day, version);
CREATE INDEX IF NOT EXISTS idx_rw_components_reading ON rw_report_components(reading_id);
CREATE TABLE IF NOT EXISTS rw_report_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_day TEXT NOT NULL,
    version INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rw_report_events_day ON rw_report_events(report_day, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _round(value: float) -> float:
    rounded = round(value, VOLUME_SCALE)
    return 0.0 if rounded == 0 else rounded


def parse_day(value: str) -> str:
    """校验 YYYY-MM-DD 自然日参数。"""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD 格式") from exc
    return parsed.date().isoformat()


def _day_bounds_utc(day: str) -> tuple[datetime, datetime]:
    start_local = datetime.fromisoformat(day).replace(tzinfo=REPORT_TZ)
    return start_local.astimezone(UTC), (start_local + timedelta(days=1)).astimezone(UTC)


def _allocations(period_start: datetime, period_end: datetime, volume: float) -> list[tuple[str, float]]:
    """把一个计量周期的水量按自然日边界（REPORT_TZ）切分，秒级均摊。"""
    start = period_start.astimezone(UTC)
    end = period_end.astimezone(UTC)
    spans: list[tuple[str, float]] = []
    cursor = start
    while cursor < end:
        local = cursor.astimezone(REPORT_TZ)
        boundary = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(UTC)
        seg_end = min(end, boundary)
        spans.append((local.date().isoformat(), (seg_end - cursor).total_seconds()))
        cursor = seg_end
    total_seconds = sum(seconds for _, seconds in spans)
    return [(day, _round(volume * seconds / total_seconds)) for day, seconds in spans]


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class RecycledWaterService:
    """再生水循环用量核算：读数受理、核查流转与版本化日报。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------ 来源

    def upsert_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO rw_sources(code,name,max_volume_m3,max_period_hours,created_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name,max_volume_m3=excluded.max_volume_m3,"
                "max_period_hours=excluded.max_period_hours,updated_at=excluded.updated_at",
                (payload["code"], payload["name"], payload.get("max_volume_m3"), payload.get("max_period_hours"), now, now),
            )
            return self._source(connection, payload["code"])  # type: ignore[return-value]

    def list_sources(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM rw_sources ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _source(connection: sqlite3.Connection, code: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM rw_sources WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ 读数

    def submit_reading(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        period_start = payload["period_start"].astimezone(UTC)
        period_end = payload["period_end"].astimezone(UTC)
        if period_end <= period_start:
            raise ValidationError("计量周期结束时间必须晚于开始时间")
        period_hours = (period_end - period_start).total_seconds() / 3600
        source = self._source(self.connection, payload["source_code"])
        status, flag_reason = self._screen(payload["volume_m3"], period_hours, source)
        is_late = 1 if now_value.astimezone(REPORT_TZ).date() > period_end.astimezone(REPORT_TZ).date() else 0
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM rw_readings WHERE client_ref=?", (payload["client_ref"],)).fetchone()
            if existing is not None:
                same = (
                    existing["source_code"] == payload["source_code"]
                    and existing["water_body"] == payload["water_body"]
                    and existing["stage"] == payload["stage"]
                    and existing["period_start"] == to_storage(period_start)
                    and existing["period_end"] == to_storage(period_end)
                    and float(existing["volume_m3"]) == float(payload["volume_m3"])
                )
                if not same:
                    raise ConflictError("同一上报键已经对应另一条读数，不能覆盖", context={"existing_reading_id": existing["id"]})
                return self._reading_detail(connection, existing["id"], deduped=True)  # type: ignore[return-value]
            cursor = connection.execute(
                "INSERT INTO rw_readings(source_code,water_body,stage,period_start,period_end,volume_m3,client_ref,"
                "status,flag_reason,submitted_by,is_late,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["source_code"], payload["water_body"], payload["stage"],
                    to_storage(period_start), to_storage(period_end), payload["volume_m3"],
                    payload["client_ref"], status, flag_reason, payload["submitted_by"], is_late, now,
                ),
            )
            reading_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO rw_reading_events(reading_id,action,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (reading_id, "received", payload["submitted_by"], "", status, flag_reason, now),
            )
            return self._reading_detail(connection, reading_id)  # type: ignore[return-value]

    @staticmethod
    def _screen(volume: float, period_hours: float, source: dict[str, Any] | None) -> tuple[str, str]:
        if volume < 0:
            return "flagged", "negative_volume:用水量为负值"
        if source is not None:
            limit = source.get("max_volume_m3")
            if limit is not None and volume > float(limit):
                return "flagged", f"above_source_limit:超过来源单周期上限 {float(limit):g} 立方米"
            max_hours = source.get("max_period_hours")
            if max_hours is not None and period_hours > float(max_hours):
                return "flagged", f"period_too_long:计量周期 {period_hours:g} 小时超过上限 {float(max_hours):g} 小时"
        return "accepted", ""

    def list_readings(self, *, status: str | None = None, source_code: str | None = None, day: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        if source_code:
            clauses.append("source_code=?")
            values.append(source_code)
        if day:
            parse_day(day)
            day_start, day_end = _day_bounds_utc(day)
            clauses.append("period_start<? AND period_end>?")
            values.extend((day_end.isoformat(), day_start.isoformat()))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(500)
        rows = self.connection.execute(
            f"SELECT * FROM rw_readings{where} ORDER BY period_start,id LIMIT ?", values
        ).fetchall()
        return [self._reading_dict(row) for row in rows]

    def get_reading(self, reading_id: int) -> dict[str, Any]:
        with transaction() as connection:
            row = connection.execute("SELECT * FROM rw_readings WHERE id=?", (reading_id,)).fetchone()
            if row is None:
                raise NotFoundError("读数不存在")
            return self._reading_detail(connection, reading_id)  # type: ignore[return-value]

    def review_reading(self, reading_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM rw_readings WHERE id=?", (reading_id,)).fetchone()
            if row is None:
                raise NotFoundError("读数不存在")
            if row["status"] != "flagged":
                raise ConflictError("只有待核查状态的读数可以确认或驳回", context={"current_status": row["status"]})
            new_status = "accepted" if payload["decision"] == "accepted" else "rejected"
            connection.execute(
                "UPDATE rw_readings SET status=?,reviewer=?,review_note=?,flag_reason=CASE WHEN ?='accepted' THEN '' ELSE flag_reason END,reviewed_at=? WHERE id=?",
                (new_status, payload["reviewer"], payload["note"], new_status, now, reading_id),
            )
            connection.execute(
                "INSERT INTO rw_reading_events(reading_id,action,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (reading_id, "review_" + payload["decision"], payload["reviewer"], "flagged", new_status, payload["note"], now),
            )
            return self._reading_detail(connection, reading_id)  # type: ignore[return-value]

    def withdraw_reading(self, reading_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM rw_readings WHERE id=?", (reading_id,)).fetchone()
            if row is None:
                raise NotFoundError("读数不存在")
            if row["status"] not in {"accepted", "flagged"}:
                raise ConflictError("只有已确认或待核查的读数可以回退", context={"current_status": row["status"]})
            connection.execute(
                "UPDATE rw_readings SET status='withdrawn',withdrawn_at=?,withdrawn_reason=? WHERE id=?",
                (now, payload["reason"], reading_id),
            )
            connection.execute(
                "INSERT INTO rw_reading_events(reading_id,action,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (reading_id, "withdrawn", payload["operator"], row["status"], "withdrawn", payload["reason"], now),
            )
            return self._reading_detail(connection, reading_id)  # type: ignore[return-value]

    def reinstate_reading(self, reading_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM rw_readings WHERE id=?", (reading_id,)).fetchone()
            if row is None:
                raise NotFoundError("读数不存在")
            if row["status"] != "withdrawn":
                raise ConflictError("只有已回退的读数可以恢复", context={"current_status": row["status"]})
            period_hours = (
                datetime.fromisoformat(row["period_end"]) - datetime.fromisoformat(row["period_start"])
            ).total_seconds() / 3600
            source = self._source(connection, row["source_code"])
            new_status, flag_reason = self._screen(float(row["volume_m3"]), period_hours, source)
            connection.execute(
                "UPDATE rw_readings SET status=?,flag_reason=?,withdrawn_at=NULL,withdrawn_reason='' WHERE id=?",
                (new_status, flag_reason, reading_id),
            )
            connection.execute(
                "INSERT INTO rw_reading_events(reading_id,action,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (reading_id, "reinstated", payload["operator"], "withdrawn", new_status, payload["reason"], now),
            )
            return self._reading_detail(connection, reading_id)  # type: ignore[return-value]

    def reopen_reading(self, reading_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM rw_readings WHERE id=?", (reading_id,)).fetchone()
            if row is None:
                raise NotFoundError("读数不存在")
            if row["status"] != "rejected":
                raise ConflictError("只有已驳回的读数可以重新提交核查", context={"current_status": row["status"]})
            reason = row["flag_reason"] or "reopened:驳回后人工发起复核"
            connection.execute(
                "UPDATE rw_readings SET status='flagged',flag_reason=?,reviewer='',review_note='',reviewed_at=NULL WHERE id=?",
                (reason, reading_id),
            )
            connection.execute(
                "INSERT INTO rw_reading_events(reading_id,action,actor,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (reading_id, "reopened", payload["operator"], "rejected", "flagged", payload["reason"], now),
            )
            return self._reading_detail(connection, reading_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ 日报

    def report_diff(self, day: str) -> dict[str, Any]:
        """最新签发版本与当前读数重算结果之间的差异，供出新版本前复核。"""
        day = parse_day(day)
        with transaction() as connection:
            key = connection.execute("SELECT latest_version FROM rw_report_keys WHERE report_day=?", (day,)).fetchone()
            live_lines, live_components = self._live_totals(connection, day)
            if key is None or int(key["latest_version"]) == 0:
                return {
                    "report_day": day,
                    "base_version": None,
                    "changed": bool(live_lines) or bool(live_components),
                    "current_lines": live_lines,
                    "previous_lines": [],
                    "line_changes": [
                        {"water_body": line["water_body"], "stage": line["stage"], "previous_volume_m3": None, "current_volume_m3": line["volume_m3"]}
                        for line in live_lines
                    ],
                    "reading_status_changes": [],
                }
            version = int(key["latest_version"])
            detail = self._version_detail(connection, day, version)
            line_changes = self._line_changes(detail["lines"], live_lines)
            status_changes = self._component_status_changes(detail["components"], live_components)
            return {
                "report_day": day,
                "base_version": version,
                "base_status": detail["status"],
                "changed": bool(line_changes) or bool(status_changes),
                "previous_lines": detail["lines"],
                "current_lines": live_lines,
                "line_changes": line_changes,
                "reading_status_changes": status_changes,
            }

    def issue_report(self, day: str, actor: str, *, confirm: bool = False) -> dict[str, Any]:
        day = parse_day(day)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute("INSERT INTO rw_report_keys(report_day,latest_version,updated_at) VALUES(?,0,?) ON CONFLICT(report_day) DO NOTHING", (day, now))
            key = connection.execute("SELECT * FROM rw_report_keys WHERE report_day=?", (day,)).fetchone()
            latest = int(key["latest_version"])
            lines, components = self._live_totals(connection, day)
            if latest:
                latest_detail = self._version_detail(connection, day, latest)
                if latest_detail["status"] == "issued":
                    line_changes = self._line_changes(latest_detail["lines"], lines)
                    status_changes = self._component_status_changes(latest_detail["components"], components)
                    if not line_changes and not status_changes:
                        raise ConflictError("当前签发版本与现有读数一致，无需生成新版本", context={"report_day": day, "latest_version": latest})
                    if not confirm:
                        raise ConflictError(
                            "版本已签发且收到会改变结果的数据，请先查看差异并以 confirm=true 签发新版本",
                            context={
                                "report_day": day,
                                "latest_version": latest,
                                "line_changes": line_changes,
                                "reading_status_changes": status_changes,
                            },
                        )
            version = latest + 1
            total = _round(sum(item["volume_m3"] for item in lines))
            digest_value = _digest(lines)
            connection.execute(
                "INSERT INTO rw_report_versions(report_day,version,status,issued_by,issued_at,total_volume_m3,content_digest) VALUES(?,?, 'issued',?,?,?,?)",
                (day, version, actor, now, total, digest_value),
            )
            for line in lines:
                connection.execute(
                    "INSERT INTO rw_report_lines(report_day,version,water_body,stage,volume_m3) VALUES(?,?,?,?,?)",
                    (day, version, line["water_body"], line["stage"], line["volume_m3"]),
                )
            for component in components:
                connection.execute(
                    "INSERT INTO rw_report_components(report_day,version,line_water_body,line_stage,reading_id,"
                    "source_code,reading_status,period_start,period_end,reading_volume_m3,signed_volume_m3) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        day, version, component["water_body"], component["stage"], component["reading_id"],
                        component["source_code"], component["reading_status"], component["period_start"],
                        component["period_end"], component["reading_volume_m3"], component["signed_volume_m3"],
                    ),
                )
            connection.execute("UPDATE rw_report_keys SET latest_version=?,updated_at=? WHERE report_day=?", (version, now, day))
            connection.execute(
                "INSERT INTO rw_report_events(report_day,version,action,actor,note,created_at) VALUES(?,?, 'issued',?,?,?)",
                (day, version, actor, f"汇总读数 {len({c['reading_id'] for c in components})} 条", now),
            )
            return self._version_detail(connection, day, version)  # type: ignore[return-value]

    def void_report(self, day: str, version: int, actor: str, reason: str) -> dict[str, Any]:
        day = parse_day(day)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM rw_report_versions WHERE report_day=? AND version=?", (day, version)
            ).fetchone()
            if row is None:
                raise NotFoundError("日报版本不存在")
            if row["status"] != "issued":
                raise ConflictError("该版本已经作废")
            key = connection.execute("SELECT latest_version FROM rw_report_keys WHERE report_day=?", (day,)).fetchone()
            if key is None or int(key["latest_version"]) != version:
                raise ConflictError("只能作废最新版本，历史版本保持原样留档")
            connection.execute(
                "UPDATE rw_report_versions SET status='voided',voided_by=?,voided_at=?,void_reason=? WHERE report_day=? AND version=?",
                (actor, now, reason, day, version),
            )
            connection.execute(
                "INSERT INTO rw_report_events(report_day,version,action,actor,note,created_at) VALUES(?,?,'voided',?,?,?)",
                (day, version, actor, reason, now),
            )
            return self._version_detail(connection, day, version)  # type: ignore[return-value]

    def current_report(self, day: str) -> dict[str, Any]:
        day = parse_day(day)
        with transaction() as connection:
            key = connection.execute("SELECT * FROM rw_report_keys WHERE report_day=?", (day,)).fetchone()
            if key is None or int(key["latest_version"]) == 0:
                live_lines, _ = self._live_totals(connection, day)
                flagged = self._flagged_count(connection, day)
                return {
                    "report_day": day,
                    "status": "unissued",
                    "latest_version": None,
                    "total_volume_m3": _round(sum(item["volume_m3"] for item in live_lines)),
                    "outdated": False,
                    "pending_flagged_readings": flagged,
                    "lines": live_lines,
                }
            version = int(key["latest_version"])
            detail = self._version_detail(connection, day, version)
            live_lines, live_components = self._live_totals(connection, day)
            detail["outdated"] = bool(self._line_changes(detail["lines"], live_lines)) or bool(
                self._component_status_changes(detail["components"], live_components)
            )
            detail["pending_flagged_readings"] = self._flagged_count(connection, day)
            return detail

    def list_versions(self, day: str) -> list[dict[str, Any]]:
        day = parse_day(day)
        rows = self.connection.execute(
            "SELECT report_day,version,status,issued_by,issued_at,voided_by,voided_at,void_reason,total_volume_m3,content_digest "
            "FROM rw_report_versions WHERE report_day=? ORDER BY version",
            (day,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_version(self, day: str, version: int) -> dict[str, Any]:
        day = parse_day(day)
        with transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM rw_report_versions WHERE report_day=? AND version=?", (day, version)
            ).fetchone()
            if row is None:
                raise NotFoundError("日报版本不存在")
            return self._version_detail(connection, day, version)  # type: ignore[return-value]

    def summary(self, from_day: str, to_day: str) -> dict[str, Any]:
        from_day = parse_day(from_day)
        to_day = parse_day(to_day)
        if from_day > to_day:
            raise ValidationError("起始日期不能晚于结束日期")
        with transaction() as connection:
            readings = connection.execute("SELECT * FROM rw_readings ORDER BY id").fetchall()
            days: list[dict[str, Any]] = []
            cursor_day = from_day
            grand_total = 0.0
            while cursor_day <= to_day:
                live_lines, live_components = self._live_totals(connection, cursor_day, readings=readings)
                key = connection.execute("SELECT latest_version FROM rw_report_keys WHERE report_day=?", (cursor_day,)).fetchone()
                version = int(key["latest_version"]) if key else 0
                if version:
                    detail = self._version_detail(connection, cursor_day, version)
                    day_total = detail["total_volume_m3"]
                    day_payload = {
                        "report_day": cursor_day,
                        "status": detail["status"],
                        "issued_version": version,
                        "issued_at": detail["issued_at"],
                        "total_volume_m3": day_total,
                        "outdated": bool(self._line_changes(detail["lines"], live_lines))
                        or bool(self._component_status_changes(detail["components"], live_components)),
                        "lines": self._lines_with_provenance(detail["lines"], detail["components"]),
                        "reading_ids": sorted({c["reading_id"] for c in detail["components"]}),
                    }
                    if detail["status"] == "voided":
                        day_payload["void_reason"] = detail["void_reason"]
                else:
                    day_total = _round(sum(item["volume_m3"] for item in live_lines))
                    day_payload = {
                        "report_day": cursor_day,
                        "status": "unissued",
                        "issued_version": None,
                        "issued_at": None,
                        "total_volume_m3": day_total,
                        "outdated": False,
                        "lines": self._lines_with_provenance(live_lines, live_components),
                        "reading_ids": sorted(
                            {item["reading_id"] for item in live_components if item["reading_status"] == "accepted"}
                        ),
                    }
                day_payload["pending_flagged_readings"] = self._flagged_count(connection, cursor_day, readings=readings)
                if day_payload["status"] == "issued":
                    grand_total += day_total
                days.append(day_payload)
                cursor_day = (datetime.fromisoformat(cursor_day) + timedelta(days=1)).date().isoformat()
            water_bodies: dict[str, float] = {}
            for day_payload in days:
                if day_payload["status"] != "issued":
                    continue
                for line in day_payload["lines"]:
                    water_bodies[line["water_body"]] = _round(water_bodies.get(line["water_body"], 0.0) + line["volume_m3"])
            return {
                "from_day": from_day,
                "to_day": to_day,
                "timezone": "UTC+08:00",
                "grand_total_m3": _round(grand_total),
                "total_by_water_body_m3": water_bodies,
                "days": days,
            }

    # ------------------------------------------------------------------ 内部

    def _live_totals(
        self, connection: sqlite3.Connection, day: str, *, readings: list[sqlite3.Row] | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按当前读数状态实时重算某日用量（已签发版本永远不被改写）。

        返回 (已确认读数汇总行, 当日全部分摊组件)；组件保留各读数当前状态，
        以便签发时完整快照待核查/驳回读数。
        """
        components = self._components_for(connection, day, readings=readings)
        lines_map: dict[tuple[str, str], float] = {}
        for component in components:
            if component["reading_status"] != "accepted":
                continue
            key = (component["water_body"], component["stage"])
            lines_map[key] = _round(lines_map.get(key, 0.0) + component["signed_volume_m3"])
        lines = [
            {"water_body": water_body, "stage": stage, "volume_m3": volume}
            for (water_body, stage), volume in sorted(lines_map.items())
        ]
        return lines, components

    @staticmethod
    def _lines_with_provenance(lines: list[dict[str, Any]], components: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给每条汇总数字挂上构成它的读数分摊明细（含状态，便于复核）。"""
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for component in components:
            grouped.setdefault((component["water_body"], component["stage"]), []).append(
                {
                    "reading_id": component["reading_id"],
                    "source_code": component["source_code"],
                    "status": component["reading_status"],
                    "period_start": component["period_start"],
                    "period_end": component["period_end"],
                    "reading_volume_m3": component["reading_volume_m3"],
                    "signed_volume_m3": component["signed_volume_m3"],
                }
            )
        result: list[dict[str, Any]] = []
        for line in lines:
            provenance = sorted(grouped.get((line["water_body"], line["stage"]), []), key=lambda item: item["reading_id"])
            result.append(
                {
                    "water_body": line["water_body"],
                    "stage": line["stage"],
                    "volume_m3": line["volume_m3"],
                    "reading_count": len(provenance),
                    "sources": provenance,
                }
            )
        return result

    @staticmethod
    def _line_changes(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        old = {(item["water_body"], item["stage"]): item["volume_m3"] for item in previous}
        new = {(item["water_body"], item["stage"]): item["volume_m3"] for item in current}
        changes: list[dict[str, Any]] = []
        for key in sorted(set(old) | set(new)):
            old_value = old.get(key)
            new_value = new.get(key)
            if old_value != new_value:
                changes.append(
                    {
                        "water_body": key[0],
                        "stage": key[1],
                        "previous_volume_m3": old_value,
                        "current_volume_m3": new_value,
                    }
                )
        return changes

    @staticmethod
    def _component_status_changes(
        snapshotted: list[dict[str, Any]], current: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        old_status = {item["reading_id"]: item["reading_status"] for item in snapshotted}
        new_status = {item["reading_id"]: item["reading_status"] for item in current}
        changes: list[dict[str, Any]] = []
        for reading_id in sorted(set(old_status) | set(new_status)):
            before = old_status.get(reading_id)
            after = new_status.get(reading_id)
            if before != after:
                changes.append({"reading_id": reading_id, "previous_status": before, "current_status": after})
        return changes

    def _components_for(
        self, connection: sqlite3.Connection, day: str, *, readings: list[sqlite3.Row] | None = None
    ) -> list[dict[str, Any]]:
        if readings is None:
            day_start, day_end = _day_bounds_utc(day)
            readings = connection.execute(
                "SELECT * FROM rw_readings WHERE period_start<? AND period_end>? ORDER BY id",
                (day_end.isoformat(), day_start.isoformat()),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in readings:
            allocations = _allocations(
                datetime.fromisoformat(row["period_start"]),
                datetime.fromisoformat(row["period_end"]),
                float(row["volume_m3"]),
            )
            for allocated_day, signed in allocations:
                if allocated_day != day:
                    continue
                result.append(
                    {
                        "water_body": row["water_body"],
                        "stage": row["stage"],
                        "reading_id": row["id"],
                        "source_code": row["source_code"],
                        "reading_status": row["status"],
                        "period_start": row["period_start"],
                        "period_end": row["period_end"],
                        "reading_volume_m3": float(row["volume_m3"]),
                        "signed_volume_m3": signed,
                    }
                )
        return result

    @staticmethod
    def _flagged_count(
        connection: sqlite3.Connection, day: str, *, readings: list[sqlite3.Row] | None = None
    ) -> int:
        if readings is None:
            day_start, day_end = _day_bounds_utc(day)
            readings = connection.execute(
                "SELECT * FROM rw_readings WHERE status='flagged' AND period_start<? AND period_end>?",
                (day_end.isoformat(), day_start.isoformat()),
            ).fetchall()
        count = 0
        for row in readings:
            if row["status"] != "flagged":
                continue
            days = {
                allocated_day
                for allocated_day, _ in _allocations(
                    datetime.fromisoformat(row["period_start"]),
                    datetime.fromisoformat(row["period_end"]),
                    float(row["volume_m3"]),
                )
            }
            if day in days:
                count += 1
        return count

    def _version_detail(self, connection: sqlite3.Connection, day: str, version: int) -> dict[str, Any]:
        header = connection.execute(
            "SELECT * FROM rw_report_versions WHERE report_day=? AND version=?", (day, version)
        ).fetchone()
        if header is None:
            raise NotFoundError("日报版本不存在")
        result = dict(header)
        line_rows = connection.execute(
            "SELECT water_body,stage,volume_m3 FROM rw_report_lines WHERE report_day=? AND version=? ORDER BY water_body,stage",
            (day, version),
        ).fetchall()
        result["lines"] = [dict(row) for row in line_rows]
        component_rows = connection.execute(
            "SELECT line_water_body AS water_body,line_stage AS stage,reading_id,source_code,reading_status,"
            "period_start,period_end,reading_volume_m3,signed_volume_m3 FROM rw_report_components "
            "WHERE report_day=? AND version=? ORDER BY line_water_body,line_stage,reading_id",
            (day, version),
        ).fetchall()
        result["components"] = [dict(row) for row in component_rows]
        return result

    @staticmethod
    def _reading_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["is_late"] = bool(data["is_late"])
        start = datetime.fromisoformat(row["period_start"])
        end = datetime.fromisoformat(row["period_end"])
        data["allocations"] = [
            {"day": day, "volume_m3": signed}
            for day, signed in _allocations(start, end, float(row["volume_m3"]))
        ]
        return data

    def _reading_detail(self, connection: sqlite3.Connection, reading_id: int, *, deduped: bool = False) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM rw_readings WHERE id=?", (reading_id,)).fetchone()
        result = self._reading_dict(row)
        events = connection.execute(
            "SELECT action,actor,from_status,to_status,note,created_at FROM rw_reading_events WHERE reading_id=? ORDER BY id",
            (reading_id,),
        ).fetchall()
        result["events"] = [dict(item) for item in events]
        included = connection.execute(
            "SELECT report_day,version,line_water_body,line_stage,signed_volume_m3,reading_status FROM rw_report_components WHERE reading_id=? ORDER BY report_day,version",
            (reading_id,),
        ).fetchall()
        result["report_refs"] = [dict(item) for item in included]
        result["deduped"] = deduped
        return result
