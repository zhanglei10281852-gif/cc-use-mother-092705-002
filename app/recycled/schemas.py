from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 读数上报
# ---------------------------------------------------------------------------


class ReadingIngest(BaseModel):
    """一套再生水处理系统上报的一个计量周期读数（周期用量，单位：立方米）。"""

    source_system: str = Field(..., min_length=1, max_length=40, description="处理系统编码，五套系统之一")
    water_body: str = Field(..., min_length=1, max_length=40, description="受纳水系，如 东湖、内河")
    process_stage: str = Field(..., min_length=1, max_length=40, description="处理环节，如 深度处理、湿地净化")
    period_start: str = Field(..., min_length=8, max_length=40, description="计量周期开始时间（ISO8601）")
    period_end: str = Field(..., min_length=8, max_length=40, description="计量周期结束时间（ISO8601）")
    volume_m3: float = Field(..., description="本周期送回水系的水量（立方米）")
    reading_key: str | None = Field(default=None, max_length=120, description="上报方幂等键，缺失时由周期内容派生")
    note: str = Field(default="", max_length=300)
    reported_at: str | None = Field(default=None, max_length=40, description="上报时间，缺省取服务端当前时间")


# ---------------------------------------------------------------------------
# 核查流转
# ---------------------------------------------------------------------------


class ReviewDecision(BaseModel):
    decision: str = Field(..., pattern="^(confirmed|rejected)$")
    reviewer: str = Field(..., min_length=1, max_length=40)
    reason: str = Field(default="", max_length=300)


# ---------------------------------------------------------------------------
# 日报签发
# ---------------------------------------------------------------------------


class IssueRequest(BaseModel):
    water_body: str = Field(..., min_length=1, max_length=40)
    usage_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    actor: str = Field(..., min_length=1, max_length=40)
