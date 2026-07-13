# -*- coding: utf-8 -*-
"""요청·응답 스키마."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Coord(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class Destination(BaseModel):
    """좌표 직접 지정 또는 POI 지정(무장애 관광지 / 대중교통 접근점)."""

    type: str = Field("coord", description="coord | tour | transit_stop | transit_station")
    poi_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class Constraints(BaseModel):
    max_slope_deg: Optional[float] = None
    avoid: Optional[List[str]] = None
    relax_if_no_route: bool = True


class PlanRequest(BaseModel):
    origin: Coord
    destination: Destination
    profile: str = "wheelchair_manual"
    constraints: Optional[Constraints] = None
    alternatives: int = Field(1, ge=1, le=3)


class RerouteRequest(BaseModel):
    current: Coord
    destination: Destination
    profile: str = "wheelchair_manual"
    route_id: Optional[str] = None


class SnapRequest(BaseModel):
    lat: float
    lng: float
    profile: Optional[str] = "wheelchair_manual"
    max_dist_m: Optional[float] = None


class RecommendRequest(BaseModel):
    disabilities: List[str] = Field(default_factory=list)
    sigungu: str = "안양"
    match_mode: str = Field("all", description="all | any")
    topk: int = Field(10, ge=1, le=50)
