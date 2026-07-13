# -*- coding: utf-8 -*-
"""경로 추천 API.

12-AccessistantAI 가 도구(Function Calling)로 호출한다.
인증: ROUTE_API_TOKEN 이 설정된 경우 Authorization: Bearer <token> 필수.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__
from ..config import get_settings
from ..engine import profiles as prof
from ..engine.access import BuildingIndex, ManualEntrances, resolve_access_point
from ..engine.graph import STORE as NET
from ..engine.planner import NoRouteError, off_route_distance_m, plan
from ..engine.snap import SnapError, snap
from ..engine.steps import build_steps
from ..poi import store as poi_store
from .schemas import (
    Destination,
    PlanRequest,
    RecommendRequest,
    RerouteRequest,
    SnapRequest,
)

logger = logging.getLogger("route_api")
settings = get_settings()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    poi_store.configure(settings)
    global BUILDINGS, ENTRANCES
    BUILDINGS = BuildingIndex(settings.buildings_path)
    ENTRANCES = ManualEntrances(settings.entrances_path)
    logger.info("접근점 데이터: 건물 %s / 실측 출입구 %d건",
                len(BUILDINGS.polys) if BUILDINGS.loaded else "없음", len(ENTRANCES.items))
    try:
        meta = NET.load(settings.network_path, settings.network_version, settings.region_name)
        logger.info(
            "네트워크 로드 완료: %s nodes=%s edges=%s",
            settings.network_path, meta["node_cnt"], meta["edge_cnt"],
        )
    except Exception as e:
        logger.warning("네트워크 로드 실패(%s) — /meta/network 로 상태 확인", e)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="IITP DABT Route API",
    version=__version__,
    description=(
        "무장애 보행 경로 추천 API. 장애 유형별 통행 프로필로 경사·계단·육교를 회피한 "
        "경로와 턴바이턴 안내를 제공한다. 대중교통은 구간 라우팅이 아니라 "
        "접근점(정류장·역)을 목적지로 다룬다."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 목적지 접근점(무장애 출입구) 해석용 — 기동 시 로드
BUILDINGS = BuildingIndex()
ENTRANCES = ManualEntrances()

# 최근 경로 캐시 (재탐색·구간 설명용) — 최대 200건
ROUTE_CACHE = OrderedDict()
CACHE_MAX = 200


def _cache_put(route_id: str, payload: dict):
    ROUTE_CACHE[route_id] = payload
    while len(ROUTE_CACHE) > CACHE_MAX:
        ROUTE_CACHE.popitem(last=False)


def auth(authorization: str = Header(default="")):
    if not settings.api_token:
        return True
    if authorization != "Bearer %s" % settings.api_token:
        raise HTTPException(status_code=401, detail="인증 토큰이 올바르지 않습니다")
    return True


# ────────────────────────── meta ──────────────────────────
@app.get("/health", tags=["meta"])
def health():
    return {
        "status": "ok" if NET.loaded else "degraded",
        "version": __version__,
        "graph_loaded": NET.loaded,
        "poi_backend": poi_store.STORE.source,
    }


@app.get("/meta/network", tags=["meta"])
def meta_network():
    if not NET.loaded:
        raise HTTPException(status_code=503, detail="네트워크가 로드되지 않았습니다")
    return NET.meta


@app.get("/profiles", tags=["meta"])
def get_profiles():
    return {"profiles": prof.list_profiles(), "default": prof.DEFAULT_PROFILE}


# ────────────────────────── route ──────────────────────────
def _profile_or_400(profile_id: str):
    try:
        return prof.get_profile(profile_id)
    except KeyError:
        raise HTTPException(status_code=400, detail="알 수 없는 프로필: %s" % profile_id)


def _resolve_destination(dest: Destination, profile) -> dict:
    """목적지 좌표 해석.

    관광지는 시설 대표점(건물 중심)이 들어오므로 그대로 쓰지 않는다.
    실측 출입구 > 건물 접근점 > 대표점 순으로 해석하고, 무엇으로 정했는지 응답에 남긴다.
    """
    if dest.type == "coord":
        if dest.lat is None or dest.lng is None:
            raise HTTPException(status_code=400, detail="목적지 좌표가 없습니다")
        return {"lat": dest.lat, "lng": dest.lng, "source": "coord"}

    if not dest.poi_id:
        raise HTTPException(status_code=400, detail="poi_id 가 필요합니다")

    if dest.type == "tour":
        manual = ENTRANCES.get(dest.poi_id)
        if manual:
            return manual
        spot = poi_store.STORE.get_tour_spot(dest.poi_id)
        if spot is None or spot["lat"] is None:
            raise HTTPException(
                status_code=404,
                detail="목적지 POI 를 찾을 수 없습니다 (poi_backend=%s)" % poi_store.STORE.source,
            )
        ent = (spot.get("entrance") or {})
        if ent.get("lat") is not None:      # 데이터 자체가 출입구 좌표를 가진 경우
            return {"lat": float(ent["lat"]), "lng": float(ent["lng"]),
                    "source": "accessible_entrance"}
        return resolve_access_point(
            NET, spot["lat"], spot["lng"], profile, BUILDINGS,
            max_walk_m=settings.entrance_max_walk_m,
        )

    resolved = poi_store.STORE.resolve_destination(dest.type, dest.poi_id)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail="목적지 POI 를 찾을 수 없습니다 (poi_backend=%s)" % poi_store.STORE.source,
        )
    return resolved


def _plan_core(origin_lat, origin_lng, dest: Destination, profile_id: str,
               alternatives: int, constraints=None) -> dict:
    if not NET.loaded:
        raise HTTPException(status_code=503, detail="네트워크가 로드되지 않았습니다")
    profile = _profile_or_400(profile_id)

    if constraints:
        from dataclasses import replace

        if constraints.max_slope_deg is not None:
            profile = replace(profile, max_slope_deg=float(constraints.max_slope_deg))
        if constraints.avoid is not None:
            profile = replace(profile, avoid=tuple(constraints.avoid))

    target = _resolve_destination(dest, profile)

    try:
        s = snap(NET, origin_lat, origin_lng, profile, settings.snap_max_dist_m)
        g = snap(NET, target["lat"], target["lng"], profile, settings.snap_max_dist_m)
    except SnapError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not s["reachable"]:
        raise HTTPException(
            status_code=422,
            detail="현재 위치가 보행 네트워크에서 %.0fm 떨어져 있어 경로를 만들 수 없습니다"
            % s["dist_m"],
        )

    relax = True if constraints is None else constraints.relax_if_no_route
    try:
        result = plan(NET, s["node_id"], g["node_id"], profile, alternatives, relax=relax)
    except NoRouteError as e:
        raise HTTPException(status_code=404, detail=str(e))

    G = NET.graph
    route_id = "r_%s" % uuid.uuid4().hex[:10]
    routes = []
    for r in result["routes"]:
        routes.append(
            {
                "summary": r["summary"],
                "geometry": r["geometry"],
                "steps": build_steps(G, r["path"], profile),
            }
        )

    payload = {
        "route_id": route_id,
        "profile": profile.id,
        "network_version": NET.meta.get("network_version"),
        "origin": {"lat": origin_lat, "lng": origin_lng,
                   "snapped": s["snapped"], "snap_dist_m": s["dist_m"]},
        "destination": {"type": dest.type, "poi_id": dest.poi_id,
                        "lat": target["lat"], "lng": target["lng"],
                        "resolved_by": target["source"],
                        "note": _entrance_note(target)},
        "routes": routes,
        "fallback": result["fallback"],
        "data_quality": {
            "slope_coverage": NET.meta.get("slope_coverage"),
            "link_type_available": NET.meta.get("link_type_available"),
        },
        "generated_at": int(time.time()),
    }
    _cache_put(route_id, payload)
    return payload


def _entrance_note(target: dict):
    """목적지 좌표를 무엇으로 정했는지 사용자에게 알린다(과신 방지)."""
    src = target.get("source")
    if src == "manual_survey":
        return "현장 실측 출입구 기준" + (" — %s" % target["note"] if target.get("note") else "")
    if src == "building_access":
        return "건물 외곽에서 보행로에 가장 가까운 지점 기준 (실제 출입구와 다를 수 있음)"
    if src == "accessible_entrance":
        return "데이터에 등록된 무장애 출입구 기준"
    if src == "facility_centroid":
        return "시설 대표 좌표 기준 — 건물 중심일 수 있으니 도착 후 출입구를 확인하세요"
    return None


@app.post("/route/plan", tags=["route"], dependencies=[Depends(auth)])
def route_plan(req: PlanRequest):
    return _plan_core(
        req.origin.lat, req.origin.lng, req.destination,
        req.profile, req.alternatives, req.constraints,
    )


@app.post("/route/reroute", tags=["route"], dependencies=[Depends(auth)])
def route_reroute(req: RerouteRequest):
    off = None
    if req.route_id and req.route_id in ROUTE_CACHE:
        geom = ROUTE_CACHE[req.route_id]["routes"][0]["geometry"]
        off = off_route_distance_m(geom, req.current.lat, req.current.lng)

    payload = _plan_core(req.current.lat, req.current.lng, req.destination, req.profile, 1, None)
    payload["off_route"] = bool(off is not None and off > settings.off_route_threshold_m)
    payload["off_route_dist_m"] = round(off, 1) if off is not None else None
    return payload


@app.post("/route/snap", tags=["route"], dependencies=[Depends(auth)])
def route_snap(req: SnapRequest):
    if not NET.loaded:
        raise HTTPException(status_code=503, detail="네트워크가 로드되지 않았습니다")
    profile = _profile_or_400(req.profile) if req.profile else None
    try:
        return snap(NET, req.lat, req.lng, profile,
                    req.max_dist_m or settings.snap_max_dist_m)
    except SnapError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/route/{route_id}", tags=["route"], dependencies=[Depends(auth)])
def route_get(route_id: str):
    payload = ROUTE_CACHE.get(route_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="경로를 찾을 수 없습니다(만료되었을 수 있음)")
    return payload


# ────────────────────────── tour ──────────────────────────
@app.get("/tour/bf-spots", tags=["tour"], dependencies=[Depends(auth)])
def tour_spots(
    sigungu: str = Query("안양", description="주소 부분일치"),
    min_lat: float = Query(None), min_lng: float = Query(None),
    max_lat: float = Query(None), max_lng: float = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    bbox = None
    if None not in (min_lat, min_lng, max_lat, max_lng):
        bbox = (min_lat, min_lng, max_lat, max_lng)
    items = poi_store.STORE.list_tour_spots(sigungu=sigungu, bbox=bbox, limit=limit)
    return {"source": poi_store.STORE.source, "count": len(items), "items": items}


@app.get("/tour/bf-spots/{poi_id}", tags=["tour"], dependencies=[Depends(auth)])
def tour_spot_detail(poi_id: str):
    item = poi_store.STORE.get_tour_spot(poi_id)
    if item is None:
        raise HTTPException(status_code=404, detail="관광지를 찾을 수 없습니다")
    return item


@app.get("/tour/bf-spots/{poi_id}/entrance", tags=["tour"], dependencies=[Depends(auth)])
def tour_spot_entrance(poi_id: str, profile: str = Query("wheelchair_manual")):
    """무장애 접근 지점. 실측 출입구 > 건물 접근점 > 시설 대표점 순으로 해석한다."""
    p = _profile_or_400(profile)
    manual = ENTRANCES.get(poi_id)
    if manual:
        manual["note"] = _entrance_note(manual)
        return manual

    spot = poi_store.STORE.get_tour_spot(poi_id)
    if spot is None or spot["lat"] is None:
        raise HTTPException(status_code=404, detail="출입구 정보를 찾을 수 없습니다")
    ent = (spot.get("entrance") or {})
    if ent.get("lat") is not None:
        return {"lat": float(ent["lat"]), "lng": float(ent["lng"]),
                "source": "accessible_entrance",
                "note": "데이터에 등록된 무장애 출입구 기준"}
    if not NET.loaded:
        raise HTTPException(status_code=503, detail="네트워크가 로드되지 않았습니다")

    acc = resolve_access_point(NET, spot["lat"], spot["lng"], p, BUILDINGS,
                               max_walk_m=settings.entrance_max_walk_m)
    acc["note"] = _entrance_note(acc)
    return acc


@app.post("/tour/recommend", tags=["tour"], dependencies=[Depends(auth)])
def tour_recommend(req: RecommendRequest):
    items = poi_store.STORE.recommend_tour(
        req.disabilities, req.sigungu, req.match_mode, req.topk
    )
    return {"source": poi_store.STORE.source, "count": len(items), "items": items}


# ────────────────────────── transit ──────────────────────────
@app.get("/transit/access-points", tags=["transit"], dependencies=[Depends(auth)])
def transit_access_points(
    lat: float = Query(...), lng: float = Query(...),
    radius_m: float = Query(800, ge=50, le=3000),
    profile: str = Query("wheelchair_manual"),
    limit: int = Query(20, ge=1, le=100),
):
    """휠체어로 접근 가능한 정류장·역. 대중교통 환승 계산은 하지 않는다."""
    _profile_or_400(profile)
    items = poi_store.STORE.list_transit_access(lat, lng, radius_m, profile, limit)
    return {"source": poi_store.STORE.source, "count": len(items), "items": items}


# ────────────────────────── admin ──────────────────────────
@app.post("/admin/reload-network", tags=["admin"], dependencies=[Depends(auth)])
def reload_network(path: str = Query(None), version: str = Query(None)):
    """그래프 교체(융기원 원본 도착 시 무중단 반영)."""
    try:
        meta = NET.load(
            path or settings.network_path,
            version or settings.network_version,
            settings.region_name,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="네트워크 파일을 찾을 수 없습니다")
    return meta
