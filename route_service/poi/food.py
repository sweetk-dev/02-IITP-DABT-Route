# -*- coding: utf-8 -*-
"""휠체어로 갈 수 있는 음식점 조회 (v1.31.0).

"휠체어로 들어갈 수 있는 식당 있어?" 에 통합DB 만으로 답한다. 원천은 둘이다.

  1) 관광 음식점 — ``mv_poi`` 의 ``search_filter.restaurant`` 행(경기관광공사, 안양 89곳).
     무장애 속성은 세 소스(경기관광공사·한국관광공사·편의시설 실태조사) 결합 결과를 쓴다.
  2) 편의시설 실태조사의 음식점 건물 — ``poi_facility_accessibility`` 의 일반음식점·휴게음식점 중
     이름이 있는 행. 건물 단위·사용승인 시점 기록이라 지금 영업 중인 가게와 다를 수 있다
     (``survey_note``). 1) 과 같은 곳이면 1) 만 남긴다.

휠체어 출입(``entry_status``)은 3상태다 — yes(턱 없는 출입구 확인) / no(턱 있음 확인) /
unknown(자료 없음). 관광 음식점은 대부분 unknown 이다(89곳 중 6곳만 무장애 속성 보유).
"정보 없음"을 "갈 수 없음"으로 말하지 않도록 응답에 그대로 싣는다.

카카오·네이버·구글 장소 정보는 쓰지 않는다 — 카카오·네이버는 편의시설 항목을 API 로 주지
않고, 구글 Places 의 휠체어 항목은 약관상 비구글 지도(카카오 지도)와 함께 쓸 수 없다
(2026-09-28 검토).
"""
from __future__ import annotations

from ..engine.geo import haversine_m
from . import buildings as poi_buildings
from . import support as poi_support
from .store import (SOURCE_LABELS, SAME_BUILDING_M, _addr_in_sigungu, _name_match_rank, _norm_name,
                    sigungu_variants)

FOOD_FACL_TYPES = ("일반음식점", "휴게음식점·제과점")
ENTRY_ORDER = {"yes": 0, "unknown": 1, "no": 2}
TOILET_PAIR_RADIUS_M = 200.0      # 식당 ↔ 접근 가능 화장실 짝짓기 반경 (v1.33.0)


def _listing_entry(spot: dict) -> str:
    """관광 음식점의 휠체어 출입 — 접근로·경사로(slope_yn)가 어느 소스든 Y 면 yes."""
    fac = spot.get("facilities") or {}
    return "yes" if fac.get("slope_yn") else "unknown"


def _fac_list(spot: dict) -> list:
    label = {"slope_yn": "접근로·경사로", "toilet_yn": "장애인 화장실", "elevator_yn": "엘리베이터",
             "parking_yn": "장애인 주차장"}
    fac = spot.get("facilities") or {}
    return [v for k, v in label.items() if fac.get(k)]


def _from_listing(spot: dict) -> dict:
    srcs = sorted({s for v in (spot.get("facility_sources") or {}).values() for s in v})
    return {
        "id": "poi:%s" % spot.get("poi_id"),
        "poi_id": spot.get("poi_id"),
        "name": spot.get("name"),
        "addr": spot.get("addr"),
        "lat": spot.get("lat"), "lng": spot.get("lng"),
        "cuisine": spot.get("category_detail"),
        "record_type": "tour_listing",
        "entry_status": _listing_entry(spot),
        "facilities": _fac_list(spot),
        "facility_sources": [SOURCE_LABELS.get(s, s) for s in srcs],
        "facility_conflicts": spot.get("facility_conflicts") or [],
        "source_label": "경기관광공사 관광 음식점",
        "survey_note": None,
    }


def _from_building(b: dict) -> dict:
    st = b["status"]
    fac = []
    if st.get("entrance_ramp") == "yes" or st.get("approach_road") == "yes":
        fac.append("접근로·경사로")
    if st.get("dis_toilet") == "yes":
        fac.append("장애인 화장실")
    if st.get("elevator") == "yes":
        fac.append("엘리베이터")
    if st.get("dis_parking") == "yes":
        fac.append("장애인 주차장")
    return {
        "id": "facl:%s" % b.get("facl_id"),
        "poi_id": None,
        "name": b["name"],
        "addr": b["addr"],
        "lat": b["lat"], "lng": b["lng"],
        "cuisine": b["facl_type"],
        "record_type": "building_survey",
        "entry_status": b["entry_status"],
        "facilities": fac,
        "facility_sources": [poi_buildings.SOURCE_LABEL],
        "facility_conflicts": [],
        "source_label": poi_buildings.SOURCE_LABEL,
        "survey_note": poi_buildings.SURVEY_NOTE,
        "base_dt": b.get("base_dt"),
    }


def _dup(b: dict, items: list) -> bool:
    nb = _norm_name(b["name"])
    for it in items:
        if it["lat"] is None or b["lat"] is None:
            continue
        d = haversine_m(b["lat"], b["lng"], it["lat"], it["lng"])
        if d <= SAME_BUILDING_M:
            return True
        if d <= 150 and _name_match_rank(nb, it.get("name")) is not None:
            return True
    return False


def attach_toilets(store, items: list, radius_m: float = TOILET_PAIR_RADIUS_M) -> list:
    """식당마다 가장 가까운 접근 가능 화장실을 붙인다 (v1.33.0, #96).

    소규모 일반음식점은 장애인등편의법상 편의시설 설치 의무 대상이 아니라 식당 자체 장애인 화장실
    자료가 거의 없다. 대신 "식당 + 반경 200m 안 접근 가능 화장실(공중·시설 내·공공건물 안)" 을
    짝지어 준다. ``toilet.status`` — own(식당 무장애 속성에 장애인 화장실) / nearby(반경 안 있음) /
    none(둘 다 없음) / unknown(좌표가 없어 찾지 못함). ``toilet.nearby`` 는 가장 가까운 1곳.

    DB 는 한 번만 부른다 — 목록 전체를 덮는 원과 반경으로 ``toilets_near`` 를 1회 호출한 뒤
    식당마다 메모리에서 최근접을 고른다(항목 수만큼 질의하지 않는다).
    """
    for it in items:
        fac = it.get("facilities")
        own = isinstance(fac, list) and "장애인 화장실" in fac
        it["toilet"] = {"status": "own" if own else "none", "nearby": None, "radius_m": radius_m}
    located = [it for it in items if it.get("lat") is not None and it.get("lng") is not None]
    for it in items:
        if it not in located and it["toilet"]["status"] != "own":
            it["toilet"]["status"] = "unknown"
    if radius_m <= 0 or not located:
        return items
    clat = sum(it["lat"] for it in located) / len(located)
    clng = sum(it["lng"] for it in located) / len(located)
    spread = max(haversine_m(clat, clng, it["lat"], it["lng"]) for it in located)
    pool = poi_support.toilets_near(store, clat, clng, spread + radius_m, 500, True)
    for it in located:
        best, best_d = None, radius_m
        for t in pool:
            if t.get("lat") is None:
                continue
            d = haversine_m(it["lat"], it["lng"], t["lat"], t["lng"])
            if d <= best_d:
                best, best_d = t, d
        if best is None:
            continue
        it["toilet"]["nearby"] = {"name": best.get("name"), "dist_m": round(best_d), "source": best.get("source"),
                                  "open_time": best.get("open_time_detail") or best.get("open_time"),
                                  "lat": best.get("lat"), "lng": best.get("lng")}
        if it["toilet"]["status"] != "own":
            it["toilet"]["status"] = "nearby"
    return items


def food_near(store, lat: float = None, lng: float = None, sigungu: str = "안양",
              radius_m: float = 3000.0, limit: int = 5, accessible_only: bool = False) -> dict:
    """음식점 목록 — 휠체어 출입 확인(yes)을 먼저, 같은 상태 안에서 가까운 순.

    반환: {items, count, total, confirmed, unknown}. total 은 반경 안 전체, confirmed 는 그중
    휠체어 출입이 확인된 곳 수 — "정보가 있는 곳이 몇 곳뿐" 을 정직하게 말하는 근거다.
    """
    items = [_from_listing(s) for s in store.list_food(sigungu)]
    # 지역 대조는 관광 음식점 목록(store.list_food)과 같은 규칙을 쓴다 — 지역명에 행정단위
    # 접미사를 붙인 토큰("안양시"/"안양군"/"안양구")이 주소에 있을 때만 그 지역으로 본다.
    # 지역명 그대로의 부분일치("안양" in 주소)는 다른 시·군의 "안양면"·"안양동" 주소까지 포함한다.
    # db 백엔드는 같은 토큰을 SQL 조건으로도 넘겨 전국 행을 읽지 않는다.
    variants = sigungu_variants(sigungu)
    rows = poi_buildings.fetch_rows(store, types=FOOD_FACL_TYPES, addr_variants=variants)
    for r in rows:
        b = poi_buildings.normalize(r)
        if b["lat"] is None or not b["named"]:
            continue
        if b["facl_type"] not in FOOD_FACL_TYPES:
            continue
        if variants and not _addr_in_sigungu(b["addr"], variants):
            continue
        if _dup(b, items):
            continue
        items.append(_from_building(b))
    near = lat is not None and lng is not None
    out = []
    for it in items:
        if it["lat"] is None:
            continue
        if near:
            d = haversine_m(lat, lng, it["lat"], it["lng"])
            if d > radius_m:
                continue
            it["dist_m"] = round(d)
        out.append(it)
    total = len(out)
    confirmed = sum(1 for it in out if it["entry_status"] == "yes")
    unknown = sum(1 for it in out if it["entry_status"] == "unknown")
    if accessible_only:
        out = [it for it in out if it["entry_status"] == "yes"]
    out.sort(key=lambda x: (ENTRY_ORDER.get(x["entry_status"], 1),
                            x["record_type"] != "tour_listing",
                            x.get("dist_m") if x.get("dist_m") is not None else 0,
                            x["name"] or ""))
    return {"items": out[:limit], "count": min(len(out), limit), "total": total,
            "confirmed": confirmed, "unknown": unknown}
