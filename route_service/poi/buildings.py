# -*- coding: utf-8 -*-
"""건물 편의시설 조회 — 장애인편의시설 실태조사(``poi_facility_accessibility``) (v1.31.0).

통합DB 에 이미 들어와 있는 한국사회보장정보원 편의시설 실태조사를 세 가지 질의에 쓴다.

  1) "○○ 휠체어로 들어갈 수 있어?" — 건물 이름·근처로 찾아 출입구·승강기·화장실·주차를 3상태로
  2) 화장실 찾기 — 청사·도서관·병원처럼 **장애인이 이용할 수 있는 화장실이 의무인 공공건물**의
     화장실을 공중화장실과 함께 안내한다(``toilets_near`` 가 합친다)
  3) 음식점 조회 — 실태조사의 음식점 건물(``food.py``)

값은 Y/N/NULL 이다. NULL 은 "없음"이 아니라 "자료 없음"(unknown)이다. 다만 실태조사 원문
(``eval_info_raw``)에는 설치된 시설 이름이 나열돼 있고 Y/N 칸만 빈 행이 있다(예: 박달복합청사 —
원문에 "장애인사용가능화장실"이 있는데 ``dis_toilet_yn`` 이 NULL). 칸이 비어 있을 때만 원문으로
보완하고, 그렇게 판정한 항목은 ``basis`` 에 ``text`` 로 남긴다.

실태조사는 건물 단위·준공(사용승인) 시점 기록이다. 음식점처럼 입점 업소가 바뀌는 건물은 지금
영업 중인 가게와 다를 수 있다 — 응답의 ``survey_note`` 로 알린다.
"""
from __future__ import annotations

import math
import re

from ..engine.geo import haversine_m
from .landmarks import clean_name, usable_name

# 필드 → 실태조사 컬럼
FIELDS = {
    "entrance_ramp": "entrance_ramp_yn",      # 주출입구 높이차이 제거(경사로 등)
    "entrance_door": "entrance_door_yn",      # 주출입구(문)
    "approach_road": "approach_road_yn",      # 주출입구 접근로
    "elevator": "elevator_yn",
    "dis_toilet": "dis_toilet_yn",
    "dis_parking": "dis_parking_yn",
    "guide_facility": "guide_facility_yn",
}
FIELD_LABEL = {
    "entrance_ramp": "주출입구 턱 없음(경사로)", "entrance_door": "주출입구 문",
    "approach_road": "주출입구 접근로", "elevator": "승강기", "dis_toilet": "장애인 화장실",
    "dis_parking": "장애인 주차구역", "guide_facility": "안내설비",
}
# 원문(eval_info_raw) 문구 → 필드. 칸이 비어 있을 때만 쓴다
EVAL_KEYWORDS = (
    ("dis_toilet", ("장애인사용가능화장실", "장애인화장실")),
    ("elevator", ("승강기", "엘리베이터")),
    ("dis_parking", ("장애인전용주차구역",)),
    ("entrance_ramp", ("높이차이 제거", "높이차이제거")),
    ("approach_road", ("주출입구 접근로", "주출입구접근로")),
    ("entrance_door", ("주출입구(문)", "주출입문")),
    ("guide_facility", ("안내설비", "점자")),
)

# 주거·생산 시설 — 불특정 다수가 드나드는 건물이 아니다
PRIVATE_TYPES = ("다세대주택", "아파트", "연립주택", "공장", "단독주택", "다가구주택", "공동주택",
                 "기숙사", "아파트 부대복리시설")
# 화장실 안내에 쓰는 공공건물 — 장애인등편의법 시행령 별표 2 에서 장애인 등이 이용 가능한
# 화장실이 의무인 공공건물 가운데, 방문객이 건물 운영시간에 이용할 수 있는 곳만 고른다.
# 학교·어린이집·노인복지시설·업무시설은 외부인 출입이 제한돼 넣지 않는다.
TOILET_PUBLIC_TYPES = (
    "국가 또는 지자체 청사", "지역자치센터", "보건소", "우체국", "파출소, 지구대", "도서관", "공공도서관",
    "종합병원", "병원·치과병원·한방병원·정신병원·요양병원", "전시장", "체육관", "장애인복지시설",
    "이외 사회복지시설", "도매시정·소매시장", "공중화장실", "국민연금공단 및 지사", "문화 및 집회시설",
)
SOURCE_LABEL = "한국사회보장정보원 장애인편의시설 실태조사"
SURVEY_NOTE = ("건물 단위 실태조사(사용승인 시점) 기록입니다. 입점 업소가 바뀌었을 수 있으니 "
               "방문 전 확인이 필요합니다.")


def _yn(v) -> str:
    if v is None:
        return "unknown"
    t = str(v).strip().upper()
    if t == "Y":
        return "yes"
    if t == "N":
        return "no"
    return "unknown"


def _f(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def facility_status(r: dict) -> tuple:
    """(필드→yes/no/unknown, 필드→column|text). 칸이 빈 항목만 원문으로 보완한다."""
    status, basis = {}, {}
    text = str(r.get("eval_info_raw") or "")
    for field, col in FIELDS.items():
        st = _yn(r.get(col))
        status[field] = st
        if st != "unknown":
            basis[field] = "column"
    for field, words in EVAL_KEYWORDS:
        if status.get(field) == "unknown" and any(w in text for w in words):
            status[field] = "yes"
            basis[field] = "text"
    return status, basis


def entry_status(status: dict) -> str:
    """휠체어로 주출입구를 들어갈 수 있는가 — 턱(높이차이) 제거가 핵심 지표다.

    yes: 턱 없음 확인 / no: 턱 있음 확인 / unknown: 자료 없음.
    접근로만 있고 턱 정보가 없으면 판단하지 않는다(unknown).
    """
    return status.get("entrance_ramp", "unknown")


def is_private_type(facl_type: str) -> bool:
    t = (facl_type or "").strip()
    return t in PRIVATE_TYPES or t.endswith("주택")


def normalize(r: dict) -> dict:
    status, basis = facility_status(r)
    name = clean_name(r.get("facl_name"))
    ftype = (r.get("facl_type") or "").strip() or None
    return {
        "facl_id": r.get("facl_id"),
        "name": name,
        "named": usable_name(name, ftype or ""),
        "facl_type": ftype,
        "addr": r.get("addr"),
        "lat": _f(r.get("latitude")), "lng": _f(r.get("longitude")),
        "status": status,
        "basis": basis,
        "entry_status": entry_status(status),
        "has": [FIELD_LABEL[k] for k, v in status.items() if v == "yes"],
        "lacks": [FIELD_LABEL[k] for k, v in status.items() if v == "no"],
        "base_dt": str(r["base_dt"]) if r.get("base_dt") is not None else None,
        "source_label": SOURCE_LABEL,
    }


def _bbox(lat, lng, radius_m):
    d_lat = float(radius_m) / 111320.0
    d_lng = d_lat / max(math.cos(math.radians(lat)), 0.01)
    return {"min_lat": lat - d_lat, "max_lat": lat + d_lat,
            "min_lng": lng - d_lng, "max_lng": lng + d_lng}


_COLS = ("facl_id, facl_name, facl_type, addr, latitude, longitude, entrance_ramp_yn, "
         "entrance_door_yn, approach_road_yn, elevator_yn, dis_toilet_yn, dis_parking_yn, "
         "guide_facility_yn, eval_info_raw, base_dt")


def fetch_rows(store, lat=None, lng=None, radius_m=None, name_q: str = "", types=None,
               addr_variants=None) -> list:
    """실태조사 행. 좌표가 있으면 bbox, 이름이 있으면 ILIKE, 유형 목록이 있으면 그 유형만.

    addr_variants: 주소에 들어 있어야 하는 시·군·구 토큰 목록(`store.sigungu_variants` 결과,
    예 ["안양시", "안양군", "안양구"]). db 백엔드에서는 SQL 조건으로 걸러 전국 행을 읽지 않는다.
    file 백엔드는 걸러 주지 않으므로 호출 측이 같은 토큰으로 한 번 더 대조한다.
    """
    if store.backend == "none":
        return []
    if store.backend == "file":
        rows = store._load_file("facility_accessibility.json")
        if types:
            rows = [r for r in rows if (r.get("facl_type") or "") in types]
        return rows
    where = ["COALESCE(del_yn, 'N') = 'N'", "latitude IS NOT NULL"]
    params = {}
    if lat is not None and lng is not None and radius_m:
        params.update(_bbox(lat, lng, radius_m))
        where.append("latitude BETWEEN :min_lat AND :max_lat AND longitude BETWEEN :min_lng AND :max_lng")
    if name_q:
        params["q"] = "%%%s%%" % name_q
        where.append("REPLACE(facl_name, ' ', '') ILIKE REPLACE(:q, ' ', '')")
    if types:
        params["types"] = list(types)
        where.append("facl_type = ANY(:types)")
    if addr_variants:
        ors = []
        for i, v in enumerate(addr_variants):
            params["ad%d" % i] = v
            ors.append("COALESCE(addr, '') LIKE '%%' || :ad{0} || '%%'".format(i))
        where.append("(%s)" % " OR ".join(ors))
    try:
        return store._query("SELECT %s FROM poi_facility_accessibility WHERE %s"
                            % (_COLS, " AND ".join(where)), params)
    except Exception:
        return []


def _name_key(s) -> str:
    return re.sub(r"[\s()（）·.,\-]|주식회사|\(주\)|㈜", "", str(s or ""))


# 같은 기관의 옛 이름·새 이름 — 실태조사에는 조사 당시 이름이 남아 있다
NAME_ALIASES = (("행정복지센터", ("주민센터", "동사무소")), ("주민센터", ("행정복지센터", "동사무소")),
                ("동사무소", ("행정복지센터", "주민센터")))


def _alias_queries(q: str) -> list:
    out = [q]
    for word, alts in NAME_ALIASES:
        if word in q:
            out += [q.replace(word, a) for a in alts]
    return out


def search(store, q: str = "", lat=None, lng=None, radius_m: float = 300.0, limit: int = 5) -> list:
    """이름(옛 이름 포함) 또는 근처로 건물 편의시설을 찾는다 — ``_search`` 참고."""
    q = (q or "").strip()
    if not q:
        return _search(store, "", lat, lng, radius_m, limit)
    for alt in _alias_queries(q):
        res = _search(store, alt, lat, lng, radius_m, limit)
        if res:
            return res
    return []


def _search(store, q: str = "", lat=None, lng=None, radius_m: float = 300.0, limit: int = 5) -> list:
    """건물 이름 또는 근처로 편의시설을 찾는다.

    이름을 주면 이름 일치(완전 > 접두 > 부분)를 먼저, 같은 등급 안에서 가까운 순.
    이름 없이 좌표만 주면 반경 안의 이름 있는 공공·상업 건물을 거리순으로.
    주거·공장은 뺀다.
    """
    q = (q or "").strip()
    key = _name_key(q)
    if q and len(key) < 2:
        return []
    near = lat is not None and lng is not None
    rows = fetch_rows(store, lat if (near and not q) else None, lng if (near and not q) else None,
                      radius_m if (near and not q) else None, name_q=q if q else "")
    out = []
    for r in rows:
        it = normalize(r)
        if it["lat"] is None or is_private_type(it["facl_type"]):
            continue
        rank = 0
        if q:
            nk = _name_key(it["name"])
            if not nk:
                continue
            if nk == key:
                rank = 0
            elif nk.startswith(key):
                rank = 1
            elif key in nk:
                rank = 2
            elif len(nk) >= 3 and nk in key:
                rank = 3
            else:
                continue
        elif not it["named"]:
            continue
        if near:
            d = haversine_m(lat, lng, it["lat"], it["lng"])
            if not q and d > radius_m:
                continue
            it["dist_m"] = round(d)
        it["match_rank"] = rank
        out.append(it)
    out.sort(key=lambda x: (x["match_rank"], x.get("dist_m") if x.get("dist_m") is not None else 0,
                            x["name"] or ""))
    return out[:limit]


def toilets_near(store, lat: float, lng: float, radius_m: float) -> list:
    """반경 안 공공건물의 장애인 화장실 — ``toilets_near`` 결과 형식에 맞춘다."""
    rows = fetch_rows(store, lat, lng, radius_m, types=TOILET_PUBLIC_TYPES)
    out = []
    for r in rows:
        it = normalize(r)
        if it["lat"] is None or it["status"].get("dis_toilet") != "yes":
            continue
        if (it["facl_type"] or "") not in TOILET_PUBLIC_TYPES or not it["named"]:
            continue
        d = haversine_m(lat, lng, it["lat"], it["lng"])
        if d > radius_m:
            continue
        public = it["facl_type"] == "공중화장실"
        out.append({
            "toilet_id": "facl:%s" % it["facl_id"],
            "name": it["name"] if public else "%s (건물 안 장애인화장실)" % it["name"],
            "facility_name": it["name"],
            "type": it["facl_type"],
            "addr": it["addr"],
            "accessible": True,
            "dis_male_cnt": None, "dis_female_cnt": None,
            "unisex": False,
            "open_time": None if public else "건물 운영시간 내",
            "open_time_detail": None,
            "emg_bell": False, "tel": None, "managing_org": None,
            "facility_toilet": not public,
            "building_toilet": not public,
            "basis": it["basis"].get("dis_toilet"),
            "source": "KOWSI_FACL",
            "source_label": SOURCE_LABEL,
            "lat": it["lat"], "lng": it["lng"],
            "dist_m": round(d),
        })
    return out
