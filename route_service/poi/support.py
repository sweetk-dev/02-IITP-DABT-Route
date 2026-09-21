# -*- coding: utf-8 -*-
"""긴급대응 지원시설·화장실 근접 조회 (#75, v1.26.0).

전동 보장구 이용자의 이동 중 방전·고장, 그리고 화장실 — 실증 자문회의에서 나온 두 생활 질의다.
데이터는 01 통합DB 의 ``poi_emergency_support``(08 이 적재: 충전기 4,053 · 수리센터 2,248)와
``poi_public_toilet_info``(안양 243) 를 그대로 읽는다. 파일 백엔드는 같은 필드의 JSON 을 쓴다.

응답은 **거리순**이고 각 행에 출처·운영시간·신뢰도를 그대로 싣는다. 운영시간이 비어 있으면
"확인 필요" 로 두지 값을 지어내지 않는다 — 수리센터 2,248건 중 운영시간이 채워진 곳은 공식
사이트로 확인된 소수뿐이다(2026-09-21).
"""
from __future__ import annotations

import math

from ..engine.geo import haversine_m

SUPPORT_TYPES = ("charge", "repair", "calltaxi")
SUPPORT_LABEL = {"charge": "전동보장구 충전기", "repair": "보장구 수리", "calltaxi": "장애인콜택시"}
SOURCE_LABEL = {
    "STD_WCHAIR_CHARGER": "행정안전부 표준데이터(전동휠체어 급속충전기)",
    "KNAT_CENTER": "중앙보조기기센터(지역 보조기기센터)",
    "KNAT_REPAIR": "지자체 수리 지원 지정업체 명부",
    "GG_ASSIST_REPAIR": "경기도 보조기기 수리 지정업체",
    "NHIS_ASSIST_STORE": "국민건강보험공단 등록업소",
    "MANUAL": "수기 등록",
}


def _bbox(lat, lng, radius_m):
    d_lat = float(radius_m) / 111320.0
    d_lng = d_lat / max(math.cos(math.radians(lat)), 0.01)
    return {"min_lat": lat - d_lat, "max_lat": lat + d_lat,
            "min_lng": lng - d_lng, "max_lng": lng + d_lng}


def _f(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _norm_support(r: dict) -> dict:
    lat, lng = _f(r.get("latitude", r.get("lat"))), _f(r.get("longitude", r.get("lng")))
    src = (r.get("source") or "").strip() or None
    hours = (r.get("open_hours") or "").strip() or None
    return {
        "support_id": r.get("support_id"),
        "support_type": r.get("support_type"),
        "type_label": SUPPORT_LABEL.get(r.get("support_type"), r.get("support_type")),
        "name": r.get("name"),
        "addr": r.get("addr_road") or r.get("addr_jibun"),
        "install_desc": r.get("install_desc") or None,
        "tel": (r.get("tel") or "").strip() or None,
        "homepage": r.get("homepage") or None,
        "open_hours": hours,
        "open_hours_status": "known" if hours else "unknown",   # unknown = 전화 확인 권장
        "note": r.get("note") or None,
        "source": src,
        "source_label": SOURCE_LABEL.get(src, src),
        "confidence": (r.get("confidence") or "").strip() or None,
        "coord_suspect": bool(r.get("coord_suspect")) if r.get("coord_suspect") is not None
                          else ("좌표 의심" in (r.get("note") or "")),
        "lat": lat, "lng": lng,
    }


def support_near(store, lat: float, lng: float, types=None, radius_m: float = 2000.0,
                 limit: int = 5) -> list:
    """반경 내 긴급대응 지원시설 — 유형별로 limit 개씩, 거리 오름차순."""
    types = [t for t in (types or SUPPORT_TYPES) if t in SUPPORT_TYPES] or list(SUPPORT_TYPES)
    if store.backend == "none":
        return []
    if store.backend == "file":
        rows = store._load_file("emergency_support.json")
    else:
        params = _bbox(lat, lng, radius_m)
        params["types"] = list(types)          # psycopg2 가 list 를 ARRAY 로 넘긴다
        rows = store._query(
            """
            SELECT support_id, support_type, name, addr_road, addr_jibun, install_desc,
                   latitude, longitude, tel, homepage, open_hours, note, source, confidence
              FROM poi_emergency_support
             WHERE COALESCE(del_yn, 'N') = 'N'
               AND support_type = ANY(:types)
               AND latitude BETWEEN :min_lat AND :max_lat
               AND longitude BETWEEN :min_lng AND :max_lng
            """,
            params,
        )
    out = []
    for r in rows:
        it = _norm_support(r)
        if it["lat"] is None or it["support_type"] not in types:
            continue
        d = haversine_m(lat, lng, it["lat"], it["lng"])
        if d > radius_m:
            continue
        it["dist_m"] = round(d)
        out.append(it)
    out.sort(key=lambda x: x["dist_m"])
    picked, cnt = [], {}
    for it in out:
        t = it["support_type"]
        if cnt.get(t, 0) >= limit:
            continue
        cnt[t] = cnt.get(t, 0) + 1
        picked.append(it)
    return picked


def _norm_toilet(r: dict) -> dict:
    lat, lng = _f(r.get("latitude", r.get("lat"))), _f(r.get("longitude", r.get("lng")))
    m_dis = int(r.get("m_dis_toilet_count") or 0) + int(r.get("m_dis_urinal_count") or 0)
    f_dis = int(r.get("f_dis_toilet_count") or 0)
    return {
        "toilet_id": r.get("toilet_id"),
        "name": r.get("toilet_name") or r.get("name"),
        "type": r.get("toilet_type"),
        "addr": r.get("addr_road") or r.get("addr_jibun"),
        "accessible": (m_dis + f_dis) > 0,          # 장애인 대·소변기 1개 이상
        "dis_male_cnt": m_dis, "dis_female_cnt": f_dis,
        "unisex": (r.get("unisex_yn") or "").upper() == "Y",
        "open_time": (r.get("open_time") or "").strip() or None,
        "open_time_detail": (r.get("open_time_detail") or "").strip() or None,
        "emg_bell": (r.get("emg_bell_yn") or "").upper() == "Y",
        "tel": (r.get("phone_number") or "").strip() or None,
        "managing_org": r.get("managing_org") or None,
        "lat": lat, "lng": lng,
    }


def toilets_near(store, lat: float, lng: float, radius_m: float = 800.0, limit: int = 5,
                 accessible_only: bool = True) -> list:
    """반경 내 공중화장실 — 기본은 장애인 화장실 보유분만, 거리 오름차순."""
    if store.backend == "none":
        return []
    if store.backend == "file":
        rows = store._load_file("public_toilets.json")
    else:
        rows = store._query(
            """
            SELECT toilet_id, toilet_name, toilet_type, addr_road, addr_jibun,
                   m_dis_toilet_count, m_dis_urinal_count, f_dis_toilet_count, unisex_yn,
                   open_time, open_time_detail, emg_bell_yn, phone_number, managing_org,
                   latitude, longitude
              FROM poi_public_toilet_info
             WHERE COALESCE(del_yn, 'N') = 'N'
               AND latitude BETWEEN :min_lat AND :max_lat
               AND longitude BETWEEN :min_lng AND :max_lng
            """,
            _bbox(lat, lng, radius_m),
        )
    out = []
    for r in rows:
        it = _norm_toilet(r)
        if it["lat"] is None:
            continue
        if accessible_only and not it["accessible"]:
            continue
        d = haversine_m(lat, lng, it["lat"], it["lng"])
        if d > radius_m:
            continue
        it["dist_m"] = round(d)
        out.append(it)
    out.sort(key=lambda x: x["dist_m"])
    return out[:limit]


def charge_hint(store, geometry: list, corridor_m: float = 1000.0, sample_every: int = 8) -> dict:
    """경로선 1km 회랑 안의 충전기 수와 최근접 1곳 — 경로 계획 응답의 한 줄 요약용.

    geometry 는 [[lat,lng],...]. 전 구간 bbox 로 한 번 조회한 뒤 표본점(sample_every)과의
    최소 거리로 회랑 판정을 한다. 실패해도 경로 계획을 깨지 않도록 호출자가 감싼다.
    """
    if not geometry or store.backend == "none":
        return {"charge_within_m": corridor_m, "charge_cnt": 0, "nearest": None}
    lats = [p[0] for p in geometry]
    lngs = [p[1] for p in geometry]
    c_lat, c_lng = (min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2
    span = max(haversine_m(min(lats), min(lngs), max(lats), max(lngs)) / 2, 1.0)
    cands = support_near(store, c_lat, c_lng, ["charge"], radius_m=span + corridor_m, limit=500)
    pts = geometry[::max(1, sample_every)] + [geometry[-1]]
    best, cnt = None, 0
    for it in cands:
        d = min(haversine_m(p[0], p[1], it["lat"], it["lng"]) for p in pts)
        if d <= corridor_m:
            cnt += 1
            if best is None or d < best["dist_to_route_m"]:
                best = {"name": it["name"], "install_desc": it["install_desc"],
                        "open_hours": it["open_hours"], "dist_to_route_m": round(d),
                        "lat": it["lat"], "lng": it["lng"]}
    return {"charge_within_m": corridor_m, "charge_cnt": cnt, "nearest": best}
