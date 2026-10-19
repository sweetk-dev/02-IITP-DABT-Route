# -*- coding: utf-8 -*-
"""장애인 문화·체육 시설 근접 조회 (v1.36.0).

01 통합DB 의 ``poi_culture_sports_facility``(08 GG_CULTURE 가 매월 적재, 경기도 전체)를 읽는다.

  braille_lib    점자도서관                  — 경기도 5곳. 안양에는 없어 가장 가까운 곳도 수원·성남이다
  booknarae_lib  책나래서비스 제공도서관     — 장애인이 국립장애인도서관 '책나래'(도서 무료 택배 대출)를
                                               신청할 수 있는 공공도서관. 260곳(안양 13)
  sports_fac     장애인체육시설              — 반다비체육센터 등 장애인 전용·우선 체육시설. 5곳
  sports_org     시군 장애인체육회           — 시설이 아니라 단체 사무국(생활체육 프로그램 문의처). 32곳

반경 안에 없을 때(폴백)
    점자도서관·체육시설은 경기도 전체에 5곳뿐이라 반경으로만 자르면 대부분 "없음" 이 된다. 이용자에게
    필요한 답은 "근처엔 없고 경기도 자료 중 가장 가까운 곳은 ○○(약 N km)" 이므로, 반경 안이 0건인 종류는
    반경 밖에서 가까운 곳을 ``beyond_radius=true`` 로 붙인다. 반경 안 결과가 1건이라도 있으면 붙이지 않는다
    (가까운 곳이 있는데 먼 곳을 섞어 혼동시키지 않기 위해서다).
    폴백에도 상한(``FALLBACK_MAX_M``, 기본 30km)을 둔다 — 그보다 먼 시설(연천·가평 등)은 교통약자에게 정보
    가치가 없어 "없음" 으로 둔다.

경기도 자료라 경기도 밖(서울 등)의 시설은 없다. 응답의 ``region_note`` 로 알린다.
표가 수백 행이라 반경 조건으로 걸러 읽지 않고 활성 행 전량을 캐시(store TTL)에 두고 거리를 계산한다.
"""
from __future__ import annotations

from ..engine.geo import haversine_m

CULTURE_TYPES = ("braille_lib", "booknarae_lib", "sports_fac", "sports_org")
CULTURE_LABEL = {
    "braille_lib": "점자도서관",
    "booknarae_lib": "책나래 신청 도서관",
    "sports_fac": "장애인체육시설",
    "sports_org": "장애인체육회",
}
# 이용자에게 한 줄로 풀어 줄 설명 — 상담 응답과 화면 카드가 같은 문구를 쓴다
CULTURE_DESC = {
    "braille_lib": "점자·음성(녹음) 도서를 빌려 주는 시각장애인 도서관",
    "booknarae_lib": "장애인이 책을 무료 택배로 빌려 보는 '책나래' 서비스를 신청할 수 있는 도서관",
    "sports_fac": "장애인이 우선 이용할 수 있는 체육시설",
    "sports_org": "장애인 생활체육 프로그램·대회 문의처(단체 사무국)",
}
SOURCE_LABEL = "경기데이터드림(경기도 장애인복지종합지원센터)"
REGION_NOTE = "경기도 자료입니다. 경기도 밖(서울 등)의 시설은 포함되지 않습니다."
CACHE_KEY = "culture_sports_facility:active:norm"
FALLBACK_MAX_M = 30000.0
FILE_NAME = "culture_sports_facility.json"


def _f(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _rows(store) -> list:
    """활성 행 전량을 정규화(_norm)해 돌려준다. 정규화 결과를 store 의 TTL 캐시에 둔다(원천은 월 1회 적재) —
    요청마다 좌표 변환·라벨 매핑을 다시 하지 않는다. 파일 백엔드도 논리삭제(del_yn='Y') 행은 뺀다."""
    if store.backend == "none":
        return []
    hit = store._cache.get(CACHE_KEY, None)
    if hit is not None:
        return hit
    if store.backend == "file":
        raw = [r for r in store._load_file(FILE_NAME) if (r.get("del_yn") or "N") != "Y"]
        rows = [_norm(r) for r in raw]
        store._cache[CACHE_KEY] = rows
        return rows
    raw = store._query(
        """
        SELECT facility_id, facility_type, sigun_nm, name, addr_road, addr_jibun,
               latitude, longitude, tel, homepage
          FROM poi_culture_sports_facility
         WHERE COALESCE(del_yn, 'N') = 'N'
        """,
        {},
    )
    rows = [_norm(r) for r in raw]
    store._cache[CACHE_KEY] = rows
    return rows


def _norm(r: dict) -> dict:
    t = r.get("facility_type")
    return {
        "facility_id": r.get("facility_id"),
        "facility_type": t,
        "type_label": CULTURE_LABEL.get(t, t),
        "type_desc": CULTURE_DESC.get(t),
        "name": r.get("name"),
        "addr": r.get("addr_road") or r.get("addr_jibun"),
        "sigun": r.get("sigun_nm") or None,
        "tel": (r.get("tel") or "").strip() or None,
        "homepage": (r.get("homepage") or "").strip() or None,
        "lat": _f(r.get("latitude", r.get("lat"))),
        "lng": _f(r.get("longitude", r.get("lng"))),
        "source_label": SOURCE_LABEL,
    }


def culture_near(store, lat: float, lng: float, types=None, radius_m: float = 5000.0,
                 limit: int = 3, fallback: bool = True, fallback_limit: int = 2,
                 fallback_max_m: float = FALLBACK_MAX_M) -> dict:
    """종류별 근접 조회.

    반환: {"items": [...], "count_by_type": {종류: 건수}, "within_by_type": {종류: 반경 안 건수}}
      items 는 종류 순서(types) → 거리 순. 각 항목에 dist_m, beyond_radius.
    """
    types = [t for t in (types or CULTURE_TYPES) if t in CULTURE_TYPES] or list(CULTURE_TYPES)
    by_type = {t: [] for t in types}
    for r in _rows(store):
        if r["facility_type"] not in by_type or r["lat"] is None or r["lng"] is None:
            continue
        it = dict(r)                      # 캐시된 정규화 행을 요청마다 복사해 거리를 붙인다(캐시 오염 방지)
        it["dist_m"] = round(haversine_m(lat, lng, it["lat"], it["lng"]))
        by_type[it["facility_type"]].append(it)
    items, count_by_type, within_by_type = [], {}, {}
    for t in types:
        cand = sorted(by_type[t], key=lambda x: (x["dist_m"], x["name"] or ""))
        within = [dict(c, beyond_radius=False) for c in cand if c["dist_m"] <= radius_m][:limit]
        within_by_type[t] = len(within)
        picked = within
        if not within and fallback:
            picked = [dict(c, beyond_radius=True) for c in cand
                      if c["dist_m"] <= max(fallback_max_m, radius_m)][:fallback_limit]
        count_by_type[t] = len(picked)
        items.extend(picked)
    return {"items": items, "count_by_type": count_by_type, "within_by_type": within_by_type}
