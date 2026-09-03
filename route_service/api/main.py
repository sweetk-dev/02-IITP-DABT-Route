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
from ..transit import gbis_live
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
    gbis_live.LIVE = gbis_live.configure(settings)
    logger.info("GBIS 실시간: %s", "사용" if gbis_live.LIVE.enabled else "인증키 없음(비활성)")
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
        "bus_realtime": gbis_live.LIVE.enabled,
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

    if dest.type == "building":
        # 이름으로 찾은 일반 시설 — 좌표가 건물 대표점이므로 출입구 해석을 거친다.
        # coord 와 달리 "이용자가 지도에서 콕 집은 점"이 아니라 "시설"이기 때문이다.
        if dest.lat is None or dest.lng is None:
            raise HTTPException(status_code=400, detail="목적지 좌표가 없습니다")
        return resolve_access_point(
            NET, dest.lat, dest.lng, profile, BUILDINGS,
            max_walk_m=settings.entrance_max_walk_m, allowed=allowed,
        )

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
    allowed = NET.reachable_nodes(profile, profile.hard_slope() + relax_margin)   # 하드 상한 기준 (v1.20.0)

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
    stop_geom = [[round(s["lat"], 7), round(s["lng"], 7)] for s in path]
    # 지도선은 GBIS 노선형상(실제 차로)을 승·하차 정류장 사이로 잘라 쓴다 (v1.20.0).
    # 형상을 못 받거나 정류장이 형상에 붙지 않으면 종전대로 정류장 직선.
    geometry, geometry_source = stop_geom, "stops"
    try:
        line = gbis_live.LIVE.route_line(route["route_id"]) if gbis_live.LIVE.enabled else []
        seg = gbis_live.LIVE.slice_line(line, part["board"], part["alight"]) if line else []
        if len(seg) >= 2:
            geometry, geometry_source = [[round(a, 7), round(b, 7)] for a, b in seg], "gbis_line"
    except Exception as e:                       # 지도선은 부가 정보 — 경로 안내를 막지 않는다
        logger.warning("노선형상 적용 실패 route_id=%s — %s", route["route_id"], e)
    dist = 0.0
    for a, b in zip(stop_geom[:-1], stop_geom[1:]):
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
        "route": {"route_id": str(route["route_id"]), "name": route.get("name"),
                  "type": route.get("type"), "end_station": route.get("end_station")},
        "board": _stop(part["board"], part["seq_from"]),
        "alight": _stop(part["alight"], part["seq_to"]),
        "stop_cnt": part["stop_cnt"],
        "stops": [{"name": s["name"], "mobile_no": s["mobile_no"], "lat": s["lat"],
                   "lng": s["lng"], "station_seq": s["station_seq"]} for s in path],
        "geometry": geometry,
        "geometry_source": geometry_source,
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
    leg_ref = {"kind": leg["kind"]}
    if leg["kind"] == "bus":
        leg_ref.update({"route_id": str(leg["route"]["route_id"]),
                        "route_name": leg["route"].get("name"),
                        "board_station_id": str(leg["board"]["poi_id"]),
                        "board_name": leg["board"]["name"],
                        "alight_station_id": str(leg["alight"]["poi_id"])})
    else:
        leg_ref.update({"line": leg.get("line"),
                        "board_station_id": str(leg["board"]["poi_id"]),
                        "alight_station_id": str(leg["alight"]["poi_id"])})
    return {"maneuver": maneuver, "instruction": instruction,
            "distance_m": 0 if not boarding else leg["est_distance_m"],
            "duration_sec": 0 if not boarding else leg["est_duration_sec"],
            "coord": [round(coord[0], 7), round(coord[1], 7)],
            "link_type": leg["kind"], "link_name": None,
            "leg_ref": leg_ref,
            "warnings": leg["warnings"] if boarding else []}


def _station_brief(poi_id: str, name: str) -> dict:
    """지하철 leg 승·하차 역의 설비 요약 — 승강기 출입구·리프트·장애인화장실(3상태)."""
    try:
        fac = poi_store.STORE.station_facilities(stn_cd=poi_id, name=name)
    except Exception as e:
        logger.warning("역 설비 조회 실패 %s: %s", name, e)
        fac = None
    if not fac:
        return {}
    return {
        "elevators": [{"exit_no": e["exit_no"], "detail_loc": e["detail_loc"]}
                      for e in fac["elevators"][:6]],
        "lifts": [{"exit_no": l["exit_no"], "detail_loc": l["detail_loc"]} for l in fac["lifts"][:4]],
        "dis_toilet": fac["status"]["dis_toilet"],
        "safety_plate": fac["status"]["safety_plate"],
        "elevator_cnt": fac["counts"]["elevator"],
        "wheelchair_lift_cnt": fac["counts"]["wheelchair_lift"],
    }


def _attach_realtime(legs: list, realtime: bool) -> None:
    """최종 선택된 legs 에 실시간·설비 정보를 붙인다.

    - 버스: realtime=true 면 승차 정류장 도착정보(해당 노선만) — 저상 차량이 확인되면
      고정 경고(LOW_BUS_WARNING)를 실측 문구로 바꾼다. 실패하면 경고를 그대로 둔다.
    - 지하철: 승·하차 역 설비 요약(정적) — 항상 붙인다(자료 없으면 빈 dict).
    """
    for leg in legs:
        if leg["kind"] == "bus":
            if not realtime:
                continue
            board = leg["board"]
            rid = leg["route"]["route_id"]
            try:
                meta = poi_store.STORE.stop_route_meta(board["poi_id"])
            except Exception:
                meta = {}
            live = gbis_live.LIVE.arrivals(board["poi_id"], route_id=rid, route_meta=meta)
            leg["realtime"] = live
            nlf = live.get("next_low_floor")
            if live.get("status") == "success":
                if nlf:
                    leg["warnings"] = [w for w in leg["warnings"] if w != LOW_BUS_WARNING]
                    leg["warnings"].insert(0, "저상버스 %s번이 약 %d분 뒤 도착 예정입니다(%s 정거장 전) — "
                                              "실시간 정보라 변동될 수 있습니다"
                                           % (nlf.get("route_name") or rid, nlf["predict_min"],
                                              nlf.get("stops_away") if nlf.get("stops_away") is not None else "?"))
                elif live.get("items"):
                    leg["warnings"] = [w for w in leg["warnings"] if w != LOW_BUS_WARNING]
                    leg["warnings"].insert(0, "지금 오는 차량은 저상버스가 아닙니다 — "
                                              "다음 저상 차량은 도착정보에서 다시 확인하세요")
        elif leg["kind"] == "subway":
            for key in ("board", "alight"):
                st = leg[key]
                st["facilities"] = _station_brief(st["poi_id"], st["name"])


def _plan_multimodal(origin_lat, origin_lng, dest: Destination, profile_id: str,
                     mode: str, constraints=None, realtime: bool = False) -> dict:
    if not NET.loaded:
        raise HTTPException(status_code=503, detail="네트워크가 로드되지 않았습니다")
    profile = _profile_or_400(profile_id)
    relax_margin = 4.0
    allowed = NET.reachable_nodes(profile, profile.hard_slope() + relax_margin)
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

    _attach_realtime(legs, realtime)

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
            req.profile, req.mode, req.constraints, realtime=req.realtime,
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
    allowed = NET.reachable_nodes(profile, profile.hard_slope() + 4.0) if profile else None
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


# ────────────────────────── poi 검색 ──────────────────────────
def _in_bbox(lat, lng) -> bool:
    """서비스 네트워크 bbox 안인지. 네트워크가 없으면 판정하지 않는다(True)."""
    if not NET.loaded:
        return True
    bb = (NET.meta or {}).get("bbox") or {}
    if bb.get("min_lat") is None:
        return True
    return (bb["min_lat"] <= lat <= bb["max_lat"]
            and bb["min_lng"] <= lng <= bb["max_lng"])


_SEARCH_TYPE_ORDER = {"tour": 0, "transit_station": 1, "building": 2}


@app.get("/poi/search", tags=["poi"], dependencies=[Depends(auth)])
def poi_search(
    q: str = Query(..., min_length=1, description="장소 이름. 예: '안양시청', '노인종합복지관'"),
    sigungu: str = Query("안양", description="관광 POI 주소 필터"),
    limit: int = Query(8, ge=1, le=30),
    include_outside: bool = Query(
        False, description="서비스 범위 밖 결과도 in_service_area=false 로 함께 준다"),
):
    """이름으로 장소를 찾아 좌표를 돌려준다 (v1.18.0).

    관광지·지하철역만으로는 이용자가 말하는 목적지(시청·복지관·도서관·학교·병원)를
    좌표로 바꿀 수 없어, 서비스 지역 안의 장소가 "지역 밖"으로 잘못 안내됐다.
    건물 폴리곤 이름(OSM)을 세 번째 출처로 더해 그 공백을 메운다.

    ``type`` 별 목적지 지정 방법:
      - ``tour``            -> ``{"type": "tour", "poi_id": ...}``
      - ``transit_station`` -> ``{"type": "transit_station", "poi_id": ...}``
      - ``building``        -> ``{"type": "building", "lat": ..., "lng": ...}``
        (건물 접근점 해석을 거치므로 좌표를 그대로 쓰는 ``coord`` 보다 낫다)
    """
    items = []
    try:
        items.extend(poi_store.STORE.search_tour_by_name(q, sigungu=sigungu, limit=limit))
    except Exception as e:                       # POI DB 미가용 — 나머지 출처로 계속
        logger.warning("관광 POI 이름 검색 실패(%s) — 건물·역 결과만 제공", e)
    try:
        items.extend(poi_store.STORE.search_stations_by_name(q, limit=limit))
    except Exception as e:
        logger.warning("역 이름 검색 실패(%s)", e)
    items.extend(BUILDINGS.search_by_name(q, limit=limit))

    out, seen = [], set()
    for it in items:
        lat, lng = it.get("lat"), it.get("lng")
        if lat is None or lng is None:
            continue
        inside = _in_bbox(lat, lng)
        # 범위 밖 결과를 그냥 버리면 소비 측은 "찾지 못했다"와 "범위 밖이다"를 구분할 수
        # 없다. 둘은 이용자에게 다르게 들려야 하는 사유이므로 표시해서 돌려준다.
        if not inside and not include_outside:
            continue
        key = (it.get("type"), str(it.get("poi_id") or ""), round(lat, 5), round(lng, 5))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "type": it.get("type") or "tour",
            "poi_id": it.get("poi_id"),
            "name": it.get("name"),
            "addr": it.get("addr"),
            "lat": lat, "lng": lng,
            "facilities": it.get("facilities"),
            "in_service_area": inside,
            "match_rank": it.get("match_rank", 9),
        })
    # 범위 안 결과를 항상 앞세운다 — 밖의 결과는 "왜 안 되는지" 를 말하기 위한 근거일 뿐이다
    out.sort(key=lambda x: (not x["in_service_area"], x["match_rank"],
                            _SEARCH_TYPE_ORDER.get(x["type"], 9),
                            len(x["name"] or ""), x["name"] or ""))
    return {
        "query": q,
        "region": (NET.meta or {}).get("region") if NET.loaded else settings.region_name,
        "source": poi_store.STORE.source,
        "buildings_loaded": bool(BUILDINGS.loaded),
        "count": len(out[:limit]),
        "items": out[:limit],
    }


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

    allowed = NET.reachable_nodes(p, p.hard_slope() + 4.0)
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


@app.get("/transit/bus/arrivals", tags=["transit"], dependencies=[Depends(auth)])
def transit_bus_arrivals(
    station_id: str = Query(..., description="GBIS 정류소 ID (tran_bus_station_info.station_id)"),
    route_id: str = Query("", description="지정 시 그 노선만 남긴다(경로 안내 중 승차 노선 확인)"),
):
    """정류장 실시간 도착정보 — 노선별 1·2번째 차량의 도착 예정(분)·정거장 수·**저상 여부**.

    `next_low_floor` 가 가장 빨리 오는 저상 차량이다. 없으면 null — "저상버스가 없다"가
    아니라 "지금 도착정보에 잡힌 두 대 안에는 없다"는 뜻이다(3번째 이후는 위치정보로 본다).
    실시간 조회에 실패하면 `status: unavailable` 로 답하고 경로 안내 자체는 막지 않는다.
    """
    try:
        meta = poi_store.STORE.stop_route_meta(station_id)
    except Exception as e:
        logger.warning("정류장 노선 메타 조회 실패 %s: %s", station_id, e)
        meta = {}
    out = gbis_live.LIVE.arrivals(station_id, route_id=route_id or None, route_meta=meta)
    out["source"] = "gbis"
    return out


@app.get("/transit/bus/locations", tags=["transit"], dependencies=[Depends(auth)])
def transit_bus_locations(
    route_id: str = Query(..., description="GBIS 노선 ID (tran_bus_route_info.route_id)"),
):
    """노선 실시간 차량 위치 — 운행 중인 전 차량의 현재 정류장(순번·이름·좌표)과 저상 여부.

    도착정보가 2대까지만 보여 주므로, 그 뒤에 오는 저상 차량을 찾거나 지도에 버스 아이콘을
    그릴 때 쓴다. 정류장 좌표는 정적 DB 의 경유정류소를 순번으로 조인한다.
    """
    try:
        idx = {s["station_id"]: s for s in poi_store.STORE.route_stops(route_id)}
    except Exception as e:
        logger.warning("노선 경유 정류장 조회 실패 %s: %s", route_id, e)
        idx = {}
    out = gbis_live.LIVE.locations(route_id, stop_index=idx)
    out["source"] = "gbis"
    return out


@app.get("/transit/station/facilities", tags=["transit"], dependencies=[Depends(auth)])
def transit_station_facilities(
    stn_cd: str = Query("", description="역 코드(poi_station_access_status.stn_cd)"),
    name: str = Query("", description="역 이름 — '범계역', '범계' 모두 가능"),
):
    """역 편의시설 — 승강기·리프트(출입구·상세위치), 화장실(게이트 안/밖·출구), 승강장
    (안전발판·스크린도어·열차 이격거리). 유무는 3상태(yes/no/unknown)다.

    개수만 있던 `poi_station_access_status` 에 설비 단위 자료(국가철도공단 파일)를 붙였다.
    "엘리베이터 3대"가 아니라 "1번 출구 옆 엘리베이터로 올라가 승강장 4-3 출입문 앞
    엘리베이터를 타라"를 말하기 위한 자료다. 실시간 가동 여부는 제공기관 API 가 없어
    싣지 않는다.
    """
    if not stn_cd and not name.strip():
        raise HTTPException(status_code=400, detail="stn_cd 또는 name 이 필요합니다")
    fac = poi_store.STORE.station_facilities(stn_cd=stn_cd, name=name)
    if fac is None:
        raise HTTPException(status_code=404, detail="역을 찾을 수 없습니다 (poi_backend=%s)"
                            % poi_store.STORE.source)
    fac["source"] = poi_store.STORE.source
    return fac


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
