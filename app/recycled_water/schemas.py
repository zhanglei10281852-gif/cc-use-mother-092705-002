from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("计量时间必须携带时区偏移，例如 2026-09-28T08:00:00+08:00")
    return value


class ReadingSubmit(BaseModel):
    """处理系统上报的一段计量周期读数。"""

    source_code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    water_body: str = Field(min_length=1, max_length=64, description="受纳水系，如 北湖、内河")
    stage: str = Field(min_length=1, max_length=64, description="处理环节，如 深度处理出水、生态补水")
    period_start: datetime = Field(description="计量周期开始时间，必须带时区偏移")
    period_end: datetime = Field(description="计量周期结束时间，必须带时区偏移")
    volume_m3: float = Field(description="周期内再生水量（立方米），异常值也必须上报，由服务转入待核查")
    client_ref: str = Field(min_length=1, max_length=80, description="上报方幂等键，重复上报同一键直接去重")
    submitted_by: str = Field(default="system", min_length=1, max_length=64)

    @field_validator("period_start", "period_end")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @field_validator("volume_m3")
    @classmethod
    def finite_volume(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("用水量必须是有限数值")
        return value

    @model_validator(mode="after")
    def _period_order(self) -> "ReadingSubmit":
        if self.period_end <= self.period_start:
            raise ValueError("计量周期结束时间必须晚于开始时间")
        return self


class ReviewDecision(BaseModel):
    decision: Literal["accepted", "rejected"]
    reviewer: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=300)


class ReadingWithdraw(BaseModel):
    reason: str = Field(min_length=2, max_length=300, description="回退原因，如 维护期表计故障")
    operator: str = Field(min_length=1, max_length=64)


class ReadingReinstate(BaseModel):
    reason: str = Field(default="", max_length=300)
    operator: str = Field(min_length=1, max_length=64)


class ReadingReopen(BaseModel):
    operator: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="驳回后人工复核", max_length=300)


class SourceUpsert(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=120)
    max_volume_m3: float | None = Field(default=None, ge=0, description="单周期合理上限，超过则读数进入待核查")
    max_period_hours: float | None = Field(default=None, gt=0, le=24 * 31, description="计量周期最长小时数")


class ReportIssue(BaseModel):
    actor: str = Field(min_length=1, max_length=64)
    confirm: bool = False


class ReportVoid(BaseModel):
    actor: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=2, max_length=300)
