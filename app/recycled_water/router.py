from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.recycled_water.schemas import (
    ReadingReinstate,
    ReadingReopen,
    ReadingSubmit,
    ReadingWithdraw,
    ReportIssue,
    ReportVoid,
    ReviewDecision,
    SourceUpsert,
)
from app.recycled_water.service import RecycledWaterService, parse_day

router = APIRouter(prefix="/api/recycled-water", tags=["再生水循环用量核算"])


def service() -> RecycledWaterService:
    return RecycledWaterService()


@router.put("/sources")
def upsert_source(payload: SourceUpsert):
    return service().upsert_source(payload.model_dump())


@router.get("/sources")
def list_sources():
    return {"items": service().list_sources()}


@router.post("/readings", status_code=201)
def submit_reading(payload: ReadingSubmit, response: Response):
    result = service().submit_reading(payload.model_dump())
    if result.get("deduped"):
        response.status_code = 200
    return result


@router.get("/readings")
def list_readings(
    status: str | None = Query(default=None, pattern="^(accepted|flagged|rejected|withdrawn)$"),
    source_code: str | None = None,
    day: str | None = None,
):
    if day:
        parse_day(day)
    return {"items": service().list_readings(status=status, source_code=source_code, day=day)}


@router.get("/readings/{reading_id}")
def get_reading(reading_id: int):
    return service().get_reading(reading_id)


@router.post("/readings/{reading_id}/review")
def review_reading(reading_id: int, payload: ReviewDecision):
    return service().review_reading(reading_id, payload.model_dump())


@router.post("/readings/{reading_id}/withdraw")
def withdraw_reading(reading_id: int, payload: ReadingWithdraw):
    return service().withdraw_reading(reading_id, payload.model_dump())


@router.post("/readings/{reading_id}/reinstate")
def reinstate_reading(reading_id: int, payload: ReadingReinstate):
    return service().reinstate_reading(reading_id, payload.model_dump())


@router.post("/readings/{reading_id}/reopen")
def reopen_reading(reading_id: int, payload: ReadingReopen):
    return service().reopen_reading(reading_id, payload.model_dump())


@router.get("/reports/{day}")
def current_report(day: str):
    return service().current_report(day)


@router.get("/reports/{day}/diff")
def report_diff(day: str):
    return service().report_diff(day)


@router.post("/reports/{day}/issue")
def issue_report(day: str, payload: ReportIssue):
    return service().issue_report(day, payload.actor, confirm=payload.confirm)


@router.get("/reports/{day}/versions")
def list_versions(day: str):
    return {"items": service().list_versions(day)}


@router.get("/reports/{day}/versions/{version}")
def get_version(day: str, version: int):
    return service().get_version(day, version)


@router.post("/reports/{day}/versions/{version}/void")
def void_version(day: str, version: int, payload: ReportVoid):
    return service().void_report(day, version, payload.actor, payload.reason)


@router.get("/summary")
def summary(from_day: str = Query(..., alias="from"), to_day: str = Query(..., alias="to")):
    return service().summary(from_day, to_day)
