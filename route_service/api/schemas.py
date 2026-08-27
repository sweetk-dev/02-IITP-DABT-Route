# -*- coding: utf-8 -*-
"""요청·응답 스키마.

응답 본문은 main.py 에서 dict 로 조립한다. v1.13.0 추가 필드(하위 호환, 추가 전용):
  - routes[].summary.crossing_point_cnt : 경로 노드에 지점 부착된 횡단보도 수
    (기존 crossing_cnt = crossing 링크 수 — 의미 불변)
  - routes[].steps[].maneuver == "crossing_point" : 노드 부착 횡단보도 안내 스텝
    (distance_m 0, crosswalk_cnt 포함. 턱낮춤 False=경고 / None="턱낮춤 미상")
"""
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
    # walk(기존, 기본) | walk_bus(직결 버스 허용) | walk_bus_subway(버스+안양 관내 지하철 허용)
    mode: str = Field("walk", description="walk | walk_bus | walk_bus_subway")


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
    # 출발지를 주면 거리 오름차순으로 정렬한다(반경 제한 없음). 목록 무한스크롤용.
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    offset: int = Field(0, ge=0, description="거리순 목록에서 건너뛸 개수")
