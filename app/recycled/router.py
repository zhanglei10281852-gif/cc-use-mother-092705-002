from __future__ import annotations

from fastapi import APIRouter, Query

from app.recycled.schemas import IssueRequest, ReadingIngest, ReviewDecision
from app.recycled.service import RecycledWaterService

router = APIRouter(prefix="/api/recycled-water", tags=["循环用水核算"])


def service() -> RecycledWaterService:
    return RecycledWaterService()


@router.post("/readings", status_code=201)
def ingest_reading(payload: ReadingIngest):
    return service().ingest_reading(payload.model_dump())


@router.get("/readings")
def list_readings(
    usage_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    water_body: str | None = None,
    status: str | None = Query(default=None, pattern=r"^(accepted|pending_review|confirmed|rejected)$"),
    limit: int = Query(default=200, ge=1, le=1000),
):
    items = service().list_readings(usage_date=usage_date, water_body=water_body, status=status, limit=limit)
    return {"items": items}


@router.get("/readings/pending-review")
def list_pending():
    return {"items": service().list_pending()}


@router.get("/readings/{reading_id}")
def get_reading(reading_id: int):
    return service().get_reading(reading_id)


@router.post("/readings/{reading_id}/review")
def review_reading(reading_id: int, payload: ReviewDecision):
    return service().review_reading(reading_id, payload.decision, payload.reviewer, payload.reason)


@router.get("/daily-report/preview")
def preview_report(water_body: str = Query(..., min_length=1), usage_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
    return service().build_daily_report(water_body, usage_date)


@router.post("/daily-report/issue", status_code=201)
def issue_report(payload: IssueRequest):
    return service().issue_daily_report(payload.water_body, payload.usage_date, payload.actor)


@router.get("/daily-report")
def get_report(
    water_body: str = Query(..., min_length=1),
    usage_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    version: int | None = Query(default=None, ge=1),
):
    return service().get_report(water_body, usage_date, version)


@router.get("/daily-report/versions")
def list_versions(water_body: str = Query(..., min_length=1), usage_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
    return {"items": service().list_report_versions(water_body, usage_date)}


@router.get("/summary")
def daily_summary(usage_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"), water_body: str | None = None):
    return service().summarize(usage_date, water_body)
