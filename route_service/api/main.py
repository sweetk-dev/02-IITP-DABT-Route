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
from ..collect import store as collect_store
from ..engine.overrides import apply_overrides
from ..poi import store as poi_store
from ..transit import planner as transit
from .schemas import (
    AccessReportRequest,
    Destination,
    PlanRequest,
    RecommendRequest,
    ReportReviewRequest,
    RerouteRequest,
    SnapRequest,
    TrackLogRequest,
)

logger = logging.getLogger("route_api")
settings = get_settings()

def _apply_overrides_safe() -> dict:
    """수집 저장소의 활성 오버라이드를 그래프에 적용 — 실패해도 서비스는 계속."""
    try:
        stat = apply_overrides(NET.graph, collect_store.STORE.active_overrides())
        logger.info("오버라이드 적용: %s", stat)
        return stat
    except Exception as e:            # DB 미가용 등 — 오버라이드 없이 운행
        logger.warning("오버라이드 적용 실패(%s) — 보정 없이 운행", e)
        return {"error": str(e)}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    poi_store.configure(settings)
    collect_store.configure(settings)
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
        _apply_overrides_safe()
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


def _resolve_destination(dest: Destination, profile, allowed=None) -> dict:
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
            max_walk_m=settings.entrance_max_walk_m, allowed=allowed,
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

    # 프로필 제약을 적용하면 보행망이 수백 조각으로 쪼개진다. 고립 조각에 스냅되면
    # 실제로는 갈 수 있는 목적지가 "경로 없음" 이 되므로, 스냅 후보를 최대 연결요소로 제한한다.
    # 제약 완화(폴백) 범위까지 고려해 여유 경사를 더한 기준으로 계산한다.
    relax_margin = 4.0 if (constraints is None or constraints.relax_if_no_route) else 0.0
    allowed = NET.reachable_nodes(profile, profile.max_slope_deg + relax_margin)

    target = _resolve_destination(dest, profile, allowed)

    try:
        s = snap(NET, origin_lat, origin_lng, profile, settings.snap_max_dist_m, allowed=allowed)
        g = snap(NET, target["lat"], target["lng"], profile, settings.snap_max_dist_m, allowed=allowed)
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


# ────────────────────────── 멀티모달 (#36) ──────────────────────────
# 제약형: 직결 버스 1회 + 안양 관내 지하철 노선 내 이동. 시각표 없음 — 소요시간 추정.
ETA_NOTE = "소요시간은 정거장 수 기반 추정이며 차량 대기 시간은 포함되지 않습니다"
LOW_BUS_WARNING = ("저상버스 정차 여부는 보장되지 않습니다 — "
                   "실시간 도착정보로 저상 차량을 확인하세요")


def _walk_leg(frm, to, profile, allowed, label_from, label_to):
    """도보 leg 1개 — 기존 보행 라우팅 재사용. 15m 미만은 leg 생략(None)."""
    straight = transit.haversine_m(frm[0], frm[1], to[0], to[1])
    if straight < 15.0:
        return None
    s = snap(NET, frm[0], frm[1], profile, settings.snap_max_dist_m, allowed=allowed)
    g = snap(NET, to[0], to[1], profile, settings.snap_max_dist_m, allowed=allowed)
    if s["node_id"] == g["node_id"]:
        return None      # 같은 노드로 스냅되는 지척 이동 — leg 생략
    result = plan(NET, s["node_id"], g["node_id"], profile, 1, relax=True)
    r = result["routes"][0]
    leg = {
        "kind": "walk",
        "from_label": label_from, "to_label": label_to,
        "summary": r["summary"],
        "geometry": r["geometry"],
        "steps": build_steps(NET.graph, r["path"], profile),
        "fallback": result["fallback"],
    }
    # 보행망 단절 가능성 — 짧은 직선을 크게 우회하면 위상 문제일 확률이 높다(#30 안양역)
    dist = r["summary"]["total_distance_m"]
    if straight < 150.0 and dist > straight * 4.0 and dist - straight > 120.0:
        leg["warnings"] = [
            "보행망 단절 가능성 — 직선 %dm 구간을 %dm 로 우회 안내합니다. "
            "현장에서 더 짧은 통로(역사 통로 등)가 있을 수 있습니다" % (round(straight), dist)
        ]
    return leg


def _bus_leg(part):
    route = part["route"]
    path = poi_store.STORE.route_stop_path(route["route_id"], part["seq_from"], part["seq_to"])
    geometry = [[round(s["lat"], 7), round(s["lng"], 7)] for s in path]
    dist = 0.0
    for a, b in zip(geometry[:-1], geometry[1:]):
        dist += transit.haversine_m(a[0], a[1], b[0], b[1])
    warnings = [LOW_BUS_WARNING]
    for key, s in (("board", part["board"]), ("alight", part["alight"])):
        if s.get("center_yn"):
            warnings.append("%s 정류장은 중앙차로 정류소입니다 — 승차장까지 횡단이 필요합니다"
                            % s["name"])
    def _stop(s, seq):
        return {"poi_id": s["poi_id"], "name": s["name"], "mobile_no": s.get("mobile_no"),
                "lat": s["lat"], "lng": s["lng"], "station_seq": seq,
                "center_yn": bool(s.get("center_yn"))}
    return {
        "kind": "bus",
        "route": {"route_id": route["route_id"], "name": route.get("name"),
                  "type": route.get("type"), "end_station": route.get("end_station")},
        "board": _stop(part["board"], part["seq_from"]),
        "alight": _stop(part["alight"], part["seq_to"]),
        "stop_cnt": part["stop_cnt"],
        "stops": [{"name": s["name"], "mobile_no": s["mobile_no"], "lat": s["lat"],
                   "lng": s["lng"], "station_seq": s["station_seq"]} for s in path],
        "geometry": geometry,
        "est_distance_m": round(dist),
        "est_duration_sec": part["stop_cnt"] * transit.BUS_SEC_PER_STOP,
        "warnings": warnings,
    }


def _subway_leg(part):
    warnings = []
    for s in (part["board"], part["alight"]):
        if not s.get("elevator_cnt"):
            warnings.append(
                "%s역은 엘리베이터 정보가 없습니다 — 휠체어 이용이 어려울 수 있으니 "
                "승강설비를 사전 확인하세요" % s["name"]
            )
    def _st(s):
        return {"poi_id": s["poi_id"], "name": s["name"], "lat": s["lat"], "lng": s["lng"],
                "elevator_cnt": s.get("elevator_cnt", 0)}
    dist = transit.haversine_m(part["board"]["lat"], part["board"]["lng"],
                               part["alight"]["lat"], part["alight"]["lng"])
    return {
        "kind": "subway",
        "line": part["line"],
        "board": _st(part["board"]), "alight": _st(part["alight"]),
        "station_cnt": part["station_cnt"],
        "geometry": [[round(part["board"]["lat"], 7), round(part["board"]["lng"], 7)],
                     [round(part["alight"]["lat"], 7), round(part["alight"]["lng"], 7)]],
        "est_distance_m": round(dist),
        "est_duration_sec": part["station_cnt"] * transit.SUBWAY_SEC_PER_STATION
                            + transit.SUBWAY_ACCESS_SEC,
        "warnings": warnings,
    }


def _transit_step(leg, boarding: bool) -> dict:
    if leg["kind"] == "bus":
        r = leg["route"]
        if boarding:
            instruction = ("%s 정류장에서 %s %s번 버스에 승차합니다 — %s 방면(경유 순번 %d번째), "
                           "%d개 정거장 이동 후 %s 하차"
                           % (leg["board"]["name"], r.get("type") or "",
                              r.get("name"), r.get("end_station") or "종점",
                              leg["board"]["station_seq"], leg["stop_cnt"],
                              leg["alight"]["name"])).replace("  ", " ")
            coord = [leg["board"]["lat"], leg["board"]["lng"]]
            maneuver = "bus_board"
        else:
            instruction = "%s 정류장에서 하차합니다" % leg["alight"]["name"]
            coord = [leg["alight"]["lat"], leg["alight"]["lng"]]
            maneuver = "bus_alight"
    else:
        if boarding:
            instruction = ("%s역에서 %s에 승차합니다 — %d개 역 이동"
                           % (leg["board"]["name"], leg["line"], leg["station_cnt"]))
            coord = [leg["board"]["lat"], leg["board"]["lng"]]
            maneuver = "subway_board"
        else:
            instruction = "%s역에서 하차합니다" % leg["alight"]["name"]
            coord = [leg["alight"]["lat"], leg["alight"]["lng"]]
            maneuver = "subway_alight"
    return {"maneuver": maneuver, "instruction": instruction,
            "distance_m": 0 if not boarding else leg["est_distance_m"],
            "duration_sec": 0 if not boarding else leg["est_duration_sec"],
            "coord": [round(coord[0], 7), round(coord[1], 7)],
            "link_type": leg["kind"], "link_name": None,
            "warnings": leg["warnings"] if boarding else []}


def _plan_multimodal(origin_lat, origin_lng, dest: Destination, profile_id: str,
                     mode: str, constraints=None) -> dict:
    if not NET.loaded:
        raise HTTPException(status_code=503, detail="네트워크가 로드되지 않았습니다")
    profile = _profile_or_400(profile_id)
    relax_margin = 4.0
    allowed = NET.reachable_nodes(profile, profile.max_slope_deg + relax_margin)
    target = _resolve_destination(dest, profile, allowed)

    cands = transit.search(
        (origin_lat, origin_lng), (target["lat"], target["lng"]), mode,
        stops_near=lambda la, ln, r: poi_store.STORE.stops_near(la, ln, r),
        stations=poi_store.STORE.stations(),
    )
    if not cands:
        raise HTTPException(
            status_code=404,
            detail="조건에 맞는 직결 대중교통 경로를 찾지 못했습니다 — 도보 경로를 이용하세요",
        )

    # 근사 스코어 순 상위 후보를 전부 실계산해 비교한다 — 도보 근사(직선×배율)와
    # 실제 휠체어 경로(계단·경사·단절 우회)의 괴리로 1순위가 최악일 수 있다(리뷰 #2).
    built_cands, last_err = [], None
    for cand in cands[:8]:      # 근사-실계산 괴리 보정 폭 — 도보 leg 계산은 저렴하다
        try:
            built = []
            for part in cand["parts"]:
                if part["kind"] == "walk":
                    leg = _walk_leg(part["frm"][1], part["to"][1], profile, allowed,
                                    part["frm"][0], part["to"][0])
                    if leg is not None:
                        built.append(leg)
                elif part["kind"] == "bus":
                    built.append(_bus_leg(part))
                else:
                    built.append(_subway_leg(part))
            actual = sum(l["summary"]["total_distance_m"] for l in built
                         if l["kind"] == "walk")
            actual += sum(l.get("stop_cnt", 0) for l in built) * transit.STOP_PENALTY_M
            actual += sum(l.get("station_cnt", 0) for l in built) * transit.STATION_PENALTY_M
            actual += sum(1 for l in built if l["kind"] != "walk") * transit.TRANSIT_LEG_PENALTY_M
            actual += 200 * sum(len(l.get("warnings", [])) for l in built if l["kind"] == "walk")
            built_cands.append((actual, built))
        except (SnapError, NoRouteError) as e:
            last_err = e
            continue
    legs = min(built_cands, key=lambda x: x[0])[1] if built_cands else None
    if legs is None:
        raise HTTPException(
            status_code=422,
            detail="대중교통 접근 도보 경로를 만들 수 없습니다 (%s)" % last_err,
        )

    # ── 통합 요약·geometry·steps ──
    walk_legs = [l for l in legs if l["kind"] == "walk"]
    transit_legs = [l for l in legs if l["kind"] != "walk"]
    walk_dist = sum(l["summary"]["total_distance_m"] for l in walk_legs)
    walk_dur = sum(l["summary"]["duration_sec"] for l in walk_legs)
    total_dist = walk_dist + sum(l["est_distance_m"] for l in transit_legs)
    total_dur = walk_dur + sum(l["est_duration_sec"] for l in transit_legs)
    warnings = sorted(set(
        w for l in walk_legs for w in l["summary"]["warnings"]
    ) | set(w for l in legs for w in l.get("warnings", [])))

    geometry, steps = [], []
    for i, leg in enumerate(legs):
        g = leg["geometry"]
        if geometry and g and geometry[-1] == g[0]:
            g = g[1:]
        geometry.extend(g)
        if leg["kind"] == "walk":
            leg_steps = leg["steps"]
            if i < len(legs) - 1 and leg_steps and leg_steps[-1]["maneuver"] == "arrive":
                leg_steps = leg_steps[:-1]      # 중간 leg 의 '도착'은 승차 안내로 대체
            steps.extend(leg_steps)
        else:
            steps.append(_transit_step(leg, boarding=True))
            steps.append(_transit_step(leg, boarding=False))
    if not steps or steps[-1]["maneuver"] != "arrive":
        last = geometry[-1] if geometry else [target["lat"], target["lng"]]
        steps.append({"maneuver": "arrive", "instruction": "목적지에 도착했습니다.",
                      "distance_m": 0, "duration_sec": 0, "coord": last,
                      "link_type": None, "link_name": None, "warnings": []})
    for idx, s in enumerate(steps):
        s["idx"] = idx

    fallback = next((l["fallback"] for l in walk_legs if l.get("fallback", {}).get("used")),
                    {"used": False})
    summary = {
        "total_distance_m": round(total_dist),
        "duration_sec": round(total_dur),
        "walk_distance_m": round(walk_dist),
        "walk_duration_sec": round(walk_dur),
        "max_slope_deg": max((l["summary"]["max_slope_deg"] for l in walk_legs), default=0),
        "stairs_cnt": sum(l["summary"]["stairs_cnt"] for l in walk_legs),
        "crossing_cnt": sum(l["summary"]["crossing_cnt"] for l in walk_legs),
        "crossing_point_cnt": sum(l["summary"].get("crossing_point_cnt", 0) for l in walk_legs),
        "transit": {
            "bus_cnt": sum(1 for l in transit_legs if l["kind"] == "bus"),
            "subway_cnt": sum(1 for l in transit_legs if l["kind"] == "subway"),
            "stop_cnt": sum(l.get("stop_cnt", 0) for l in transit_legs),
            "station_cnt": sum(l.get("station_cnt", 0) for l in transit_legs),
        },
        "eta_note": ETA_NOTE,
        "warnings": warnings,
    }

    route_id = "r_%s" % uuid.uuid4().hex[:10]
    payload = {
        "route_id": route_id,
        "profile": profile.id,
        "mode": mode,
        "network_version": NET.meta.get("network_version"),
        "origin": {"lat": origin_lat, "lng": origin_lng},
        "destination": {"type": dest.type, "poi_id": dest.poi_id,
                        "lat": target["lat"], "lng": target["lng"],
                        "resolved_by": target["source"],
                        "note": _entrance_note(target)},
        "routes": [{"summary": summary, "geometry": geometry, "steps": steps, "legs": legs}],
        "fallback": fallback,
        "data_quality": {
            "slope_coverage": NET.meta.get("slope_coverage"),
            "link_type_available": NET.meta.get("link_type_available"),
        },
        "generated_at": int(time.time()),
    }
    for leg in legs:                     # 내부 필드 정리
        leg.pop("fallback", None)
    _cache_put(route_id, payload)
    return payload


@app.post("/route/plan", tags=["route"], dependencies=[Depends(auth)])
def route_plan(req: PlanRequest):
    if req.mode in ("walk_bus", "walk_bus_subway"):
        return _plan_multimodal(
            req.origin.lat, req.origin.lng, req.destination,
            req.profile, req.mode, req.constraints,
        )
    if req.mode not in ("", "walk", None):
        raise HTTPException(status_code=400,
                            detail="지원하지 않는 mode 입니다: %s" % req.mode)
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
    allowed = NET.reachable_nodes(profile, profile.max_slope_deg + 4.0) if profile else None
    try:
        return snap(NET, req.lat, req.lng, profile,
                    req.max_dist_m or settings.snap_max_dist_m, allowed=allowed)
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

    allowed = NET.reachable_nodes(p, p.max_slope_deg + 4.0)
    acc = resolve_access_point(NET, spot["lat"], spot["lng"], p, BUILDINGS,
                               max_walk_m=settings.entrance_max_walk_m, allowed=allowed)
    acc["note"] = _entrance_note(acc)
    return acc


@app.post("/tour/recommend", tags=["tour"], dependencies=[Depends(auth)])
def tour_recommend(req: RecommendRequest):
    items = poi_store.STORE.recommend_tour(
        req.disabilities, req.sigungu, req.match_mode, req.topk,
        origin_lat=req.origin_lat, origin_lng=req.origin_lng, offset=req.offset,
    )
    # total 은 클라이언트가 무한스크롤 종료를 판단하는 근거 — offset+topk 로는 알 수 없다.
    total = len(poi_store.STORE.recommend_tour(
        req.disabilities, req.sigungu, req.match_mode, 10000,
        origin_lat=req.origin_lat, origin_lng=req.origin_lng, offset=0,
    ))
    return {"source": poi_store.STORE.source, "count": len(items),
            "total": total, "offset": req.offset,
            "has_more": req.offset + len(items) < total, "items": items}


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


# ────────────────────────── collect (수집 장치화) ──────────────────────────
# 실증 자체가 수집 장치다: 안내 세션의 GPS 트랙과 원터치 오류 제보가
# 서비스 이용의 부산물로 쌓인다. 참여자 식별자는 받지도 저장하지도 않는다.


@app.post("/track/log", tags=["collect"], dependencies=[Depends(auth)])
def track_log(req: TrackLogRequest):
    """주행 GPS 트랙 배치 업로드 (안내 세션 종료 시 1회 권장, 중복 seq 는 무시)."""
    if len(req.points) > collect_store.MAX_POINTS_PER_CALL:
        raise HTTPException(status_code=422,
                            detail="한 번에 %d점 이하로 나눠 올려주세요"
                                   % collect_store.MAX_POINTS_PER_CALL)
    try:
        stored = collect_store.STORE.log_track(
            req.route_id,
            [p.model_dump() for p in req.points],
            req.meta.model_dump() if req.meta else None,
        )
    except Exception as e:
        logger.warning("트랙 저장 실패: %s", e)
        raise HTTPException(status_code=503, detail="트랙 저장소를 사용할 수 없습니다")
    return {"route_id": req.route_id, "stored_points": stored}


@app.post("/report/accessibility", tags=["collect"], dependencies=[Depends(auth)])
def report_accessibility(req: AccessReportRequest):
    """접근성 오류 제보 — 접수 즉시 해당 지점 링크에 '이용자 제보(미확인)' 경고가 붙는다."""
    try:
        out = collect_store.STORE.add_report(
            req.lat, req.lng, req.reason, req.detail, req.route_id,
            req.photo_base64, req.photo_mime,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.warning("제보 저장 실패: %s", e)
        raise HTTPException(status_code=503, detail="제보 저장소를 사용할 수 없습니다")
    if NET.loaded:
        _apply_overrides_safe()       # 수위 1: 경고는 즉시 안내에 반영
    return {**out, "message": "제보가 접수되었습니다. 확인 후 경로 안내에 반영됩니다."}


@app.get("/report/accessibility", tags=["collect"], dependencies=[Depends(auth)])
def list_accessibility_reports(
    status: str = Query(None, description="new | confirmed | rejected | applied"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """제보 목록 (관리 콘솔용, 사진은 별도 엔드포인트)."""
    items = collect_store.STORE.list_reports(status, limit, offset)
    return {"count": len(items), "items": items,
            "reasons": collect_store.REASONS}


@app.get("/report/accessibility/{report_id}/photo", tags=["collect"],
         dependencies=[Depends(auth)])
def report_photo(report_id: int):
    photo, mime = collect_store.STORE.get_report_photo(report_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="사진이 없습니다")
    from fastapi.responses import Response
    return Response(content=bytes(photo), media_type=mime or "image/jpeg")


@app.patch("/report/accessibility/{report_id}", tags=["collect"],
           dependencies=[Depends(auth)])
def review_accessibility_report(report_id: int, req: ReportReviewRequest):
    """관리자 검토 — confirm(경고 확정) / reject(경고 철회) / apply(속성 반영, 승인제)."""
    try:
        out = collect_store.STORE.review_report(
            report_id, req.action, req.attr, req.value, req.note, req.radius_m)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    stat = _apply_overrides_safe() if NET.loaded else {}
    return {**out, "overrides": stat}


@app.delete("/report/accessibility/{report_id}", tags=["collect"],
            dependencies=[Depends(auth)])
def delete_accessibility_report(report_id: int):
    """제보 삭제 — 오검·중복·시험 제보 정리용. 파생 오버라이드도 함께 사라진다.

    검토(PATCH reject)는 '확인했고 사실이 아님'을 남기는 기록이고, 이쪽은 기록 자체를
    지운다. 삭제 즉시 오버라이드를 재적용해 그래프에서 경고를 걷어낸다.
    """
    try:
        out = collect_store.STORE.delete_report(report_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.warning("제보 삭제 실패: %s", e)
        raise HTTPException(status_code=503, detail="제보 저장소를 사용할 수 없습니다")
    stat = _apply_overrides_safe() if NET.loaded else {}
    return {**out, "overrides": stat}


# ────────────────────────── admin ──────────────────────────
@app.post("/admin/reload-network", tags=["admin"], dependencies=[Depends(auth)])
def reload_network(path: str = Query(None), version: str = Query(None)):
    """그래프 교체(융기원 원본 도착 시 무중단 반영). 오버라이드도 재적용된다."""
    try:
        meta = NET.load(
            path or settings.network_path,
            version or settings.network_version,
            settings.region_name,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="네트워크 파일을 찾을 수 없습니다")
    meta["overrides"] = _apply_overrides_safe()
    return meta


@app.post("/admin/reload-overrides", tags=["admin"], dependencies=[Depends(auth)])
def reload_overrides():
    """오버라이드만 재적용 (그래프 로드 없이). 콘솔에서 일괄 검토 후 호출."""
    if not NET.loaded:
        raise HTTPException(status_code=503, detail="네트워크가 로드되지 않았습니다")
    return _apply_overrides_safe()
