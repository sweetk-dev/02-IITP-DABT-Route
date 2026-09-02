# -*- coding: utf-8 -*-
"""POI 조회 — 무장애 관광지 / 대중교통 접근점.

백엔드 3종 (POI_BACKEND 환경변수)
  db   : 01-IITP-DABT-Database 읽기 전용 조회
         mv_poi (무장애 관광지 정본) / poi_station_access_status /
         poi_station_wheelchair_lift / poi_facility_accessibility
  file : POI_DATA_DIR 아래 JSON (오프라인·개발·데모용)
  none : 빈 결과 (source="none") — 적재 전에도 서비스가 기동되도록

⚠️ 데이터 적재 현황: 이동편의 데이터 파이프라인(01 테이블 + 08 어댑터)이 적재 완료되기
전까지 db 백엔드는 빈 결과를 낼 수 있다. 응답의 source 필드로 클라이언트가 구분한다.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re

from ..engine.geo import haversine_m

logger = logging.getLogger("route_api.poi")

# 01-IITP-DABT-Database `poi_tour_bf_facility` 실제 컬럼 (2026-07-13 실측)
TOUR_FIELDS = [
    "toilet_yn", "elevator_yn", "parking_yn", "slope_yn", "subway_yn", "bus_stop_yn",
    "wheelchair_rent_yn", "tactile_map_yn", "audio_guide_yn", "nursing_room_yn",
    "accessible_room_yn", "stroller_rent_yn",
]

# mv_poi.detail_json.accessible_facilities 의 표기 -> TOUR_FIELDS 매핑 (2026-07-16 전수 실측)
# 통합DB 는 시설을 한국어 문구로 담고 있어 기존 *_yn 필드 체계로 정규화한다.
MVPOI_FACILITY_MAP = {
    "장애인 전용 통로 있음": "toilet_yn",        # 화장실 접근 동선 — 대표 지표로 사용
    "엘리베이터 있음": "elevator_yn",
    "장애인 주차장 있음": "parking_yn",
    "접근로 있음": "slope_yn",
    "대중교통 이용 가능": "subway_yn",
    "휠체어 대여 가능": "wheelchair_rent_yn",
    "점자블록 있음": "tactile_map_yn",
    "점자 홍보물 있음": "tactile_map_yn",
    "유도안내설비 있음": "tactile_map_yn",
    "오디오가이드 있음": "audio_guide_yn",
    "안내요원 있음": "audio_guide_yn",
    "수화안내 가능": "audio_guide_yn",
    "자막 지원 가능": "audio_guide_yn",
    "장애인 객실 있음": "accessible_room_yn",
    "지체장애인 관람석 있음": "accessible_room_yn",
}

# 장애 유형 -> 필요한 편의시설 필드 (10-TripSense 매칭 로직과 동일한 사고)
DISABILITY_REQUIREMENTS = {
    "지체장애": ["toilet_yn", "elevator_yn", "parking_yn", "slope_yn", "wheelchair_rent_yn"],
    "휠체어": ["toilet_yn", "elevator_yn", "slope_yn"],
    "시각장애": ["tactile_map_yn", "audio_guide_yn"],
    "청각장애": ["audio_guide_yn"],
    "영유아동반": ["nursing_room_yn", "stroller_rent_yn"],
}


def _yn3(v) -> str:
    """Y/N/NULL 을 yes/no/unknown 으로. NULL 은 "없음"이 아니라 "자료 없음"이다."""
    if v is None:
        return "unknown"
    t = str(v).strip().upper()
    if t in ("Y", "YES", "TRUE", "1"):
        return "yes"
    if t in ("N", "NO", "FALSE", "0"):
        return "no"
    return "unknown"


def _is_y(v) -> bool:
    return str(v).strip().upper() in ("Y", "TRUE", "1")


# 시·군·구 행정단위 접미사. "안양" 처럼 접미사 없이 들어온 지역명은 이 접미사를 붙인
# 변형("안양시"/"안양군"/"안양구")으로만 주소를 대조한다. 단순 부분일치는 전남 장흥군
# "안양면" 같은 타지역 주소까지 끌어온다(#26).
_SIGUNGU_SUFFIXES = ("시", "군", "구")


def _normalize_routes(file_routes, db_routes) -> list:
    """경유 노선을 ``{route_id, name, type, end_station, station_seq}`` 로 정규화한다.

    노선번호만으로는 노선을 특정할 수 없다 — 안양 연관 117개 노선 중 **번호가
    겹치는 것이 12쌍**이다(실측). 예를 들어 "2" 는 일반형시내버스 213000017 과
    마을버스 241253001 둘 다이며, 김중업 건축박물관을 지나는 것은 후자뿐이다.
    번호만 노출하면 정류장에서 다른 버스를 타는 사고로 이어지므로 유형을 함께 준다.

    ``end_station`` 은 노선의 종점명이다. 이용자가 정류장 안내판에서 바로 대조할
    수 있는 방면 정보이며, 아래 ``station_seq`` 산술 없이도 방향을 가늠하게 한다.

    ``station_seq`` 는 그 노선이 이 정류장을 몇 번째로 지나는지다. 회차 노선은 한
    정류장을 두 번 지나므로 값이 여러 개일 수 있다(예: 마을버스 2번은 안양역을
    순번 10 과 34 에서 지난다). 승차·하차 정류장의 순번을 비교하면 진행 방향을
    판정할 수 있다 — 이름이 같은 양방향 정류장에서 반대편 차를 타는 것을 막는다.

    다만 순번 비교에는 한계가 있다. 순환 노선은 승차 순번이 하차 순번보다 클 수
    있고(종점을 지나 계속 운행), 양쪽 값이 여럿이면 조합이 여러 개 나온다. 소비
    측은 배열을 그대로 비교하지 말고 각 조합을 검토해야 하며, 확정이 어려우면
    ``end_station`` 을 함께 안내해 이용자가 현장에서 대조하도록 해야 한다.

    file 백엔드는 문자열 목록(``["2", "11"]``)도 허용한다 — 이 경우 유형은 None.
    """
    rows = db_routes if db_routes is not None else file_routes
    if not rows:
        return []
    if isinstance(rows, str):
        # 드라이버가 json 을 디코드하지 않고 문자열로 넘기는 경우를 방어한다.
        text = rows.strip()
        if text.startswith("["):
            try:
                rows = json.loads(text)
            except ValueError:
                rows = []
        else:
            rows = [x for x in text.split(",") if x]
    out = []
    for r in rows:
        if isinstance(r, dict):
            name = r.get("name") or r.get("route_name")
            if not name:
                continue
            seq = r.get("station_seq")
            if seq is None:
                seq = []
            elif not isinstance(seq, list):
                seq = [seq]
            out.append({"route_id": r.get("route_id"), "name": str(name),
                        "type": r.get("type") or r.get("route_type_name"),
                        "end_station": (r.get("end_station")
                                        or r.get("end_station_name")),
                        "station_seq": [x for x in seq if x is not None]})
        else:
            out.append({"route_id": None, "name": str(r), "type": None,
                        "end_station": None, "station_seq": []})
    seen, uniq = set(), []
    for r in out:
        key = (r["route_id"], r["name"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda x: (x["name"], x["route_id"] or 0))
    return uniq


def sigungu_variants(sigungu: str) -> list:
    """지역명 -> 주소 대조용 행정단위 토큰 목록.

    "안양"   -> ["안양시", "안양군", "안양구"]
    "안양시" -> ["안양시"]  (이미 행정단위 접미사가 있으면 그대로)
    """
    s = (sigungu or "").strip()
    if not s:
        return []
    if s.endswith(_SIGUNGU_SUFFIXES):
        return [s]
    return [s + suf for suf in _SIGUNGU_SUFFIXES]


def _addr_in_sigungu(addr: str, variants: list) -> bool:
    a = addr or ""
    return any(v in a for v in variants)


def _norm_name(s: str) -> str:
    """이름 대조용 정규화 — 공백·가운뎃점·괄호류 제거."""
    return re.sub(r"[\s\-_·.,()]+", "", str(s or ""))


def _name_match_rank(nq: str, name: str):
    """완전일치 0 / 접두 1 / 부분 2 / 역방향 3 — 매칭 실패는 None."""
    nn = _norm_name(name)
    if not nn or not nq:
        return None
    if nn == nq:
        return 0
    if nn.startswith(nq):
        return 1
    if nq in nn:
        return 2
    if len(nn) >= 3 and nn in nq:
        return 3
    return None


class PoiStore:
    def __init__(self, backend: str = "none", data_dir: str = "", dsn: str = ""):
        self.backend = backend
        self.data_dir = data_dir
        self.dsn = dsn
        self._engine = None
        self._cache = {}

    # ---------- 공통 ----------
    @property
    def source(self) -> str:
        return self.backend

    def _load_file(self, name: str) -> list:
        if name in self._cache:
            return self._cache[name]
        path = os.path.join(self.data_dir, name)
        if not os.path.exists(path):
            self._cache[name] = []
            return []
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        rows = rows if isinstance(rows, list) else rows.get("items", [])
        self._cache[name] = rows
        return rows

    def _db(self):
        if self._engine is None:
            from sqlalchemy import create_engine

            self._engine = create_engine(self.dsn, pool_pre_ping=True, future=True)
        return self._engine

    def _query(self, sql: str, params: dict) -> list:
        from sqlalchemy import text

        with self._db().connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    # ---------- 무장애 관광지 ----------
    def list_tour_spots(self, sigungu: str = "", bbox=None, limit: int = 50) -> list:
        if self.backend == "none":
            return []
        variants = sigungu_variants(sigungu)
        if self.backend == "file":
            rows = self._load_file("tour_bf.json")
        else:
            # 지역 필터는 주소의 시·군·구 토큰 정합으로만 판정한다.
            # LIKE '%안양%' 방식은 주소 전체를 보기 때문에 전남 장흥군 "안양면" 소재
            # POI(거리 307km)까지 포함시켰다(#26). title 대조도 같은 이유로 지역
            # 필터에서 제외한다(이름 검색은 get_tour_spot 의 이름 폴백이 담당).
            sg_clause = ""
            params = {"limit": limit}
            if variants:
                ors = []
                for i, v in enumerate(variants):
                    ors.append(
                        "COALESCE(address_road, '') LIKE '%%' || :sg{0} || '%%'"
                        " OR COALESCE(address_detail, '') LIKE '%%' || :sg{0} || '%%'".format(i)
                    )
                    params["sg%d" % i] = v
                sg_clause = "AND (%s)" % " OR ".join(ors)
            rows = self._query(
                """
                SELECT poi_id,
                       title AS name,
                       COALESCE(address_road, address_detail) AS addr,
                       latitude, longitude,
                       detail_json->>'accessible_facilities' AS fac_text,
                       search_filter_json->'search_filter'->>'tourist_type' AS tourist_type
                  FROM mv_poi
                 WHERE language_code = 'ko'
                   AND latitude IS NOT NULL AND longitude IS NOT NULL
                   AND COALESCE(detail_json->>'accessible_facilities', '') <> ''
                   {sg}
                 LIMIT :limit
                """.format(sg=sg_clause),
                params,
            )
        out = [self._normalize_tour(r) for r in rows]
        if variants and self.backend == "file":
            out = [r for r in out if _addr_in_sigungu(r.get("addr"), variants)]
        if bbox:
            min_lat, min_lng, max_lat, max_lng = bbox
            out = [
                r for r in out
                if r["lat"] is not None
                and min_lat <= r["lat"] <= max_lat
                and min_lng <= r["lng"] <= max_lng
            ]
        return out[:limit]

    @staticmethod
    def _facilities_from_text(text: str) -> dict:
        """mv_poi 의 한국어 시설 문구 -> *_yn 불리언 맵."""
        fac = {k: False for k in TOUR_FIELDS}
        for part in (text or "").split(","):
            key = MVPOI_FACILITY_MAP.get(part.strip())
            if key:
                fac[key] = True
        return fac

    @staticmethod
    def _normalize_tour(r: dict) -> dict:
        lat = r.get("latitude", r.get("lat"))
        lng = r.get("longitude", r.get("lng"))
        addr = r.get("addr") or r.get("addr_road") or r.get("addr_jibun")
        if r.get("fac_text") is not None:
            fac = PoiStore._facilities_from_text(r.get("fac_text"))
        else:
            fac = {k: _is_y(r.get(k)) for k in TOUR_FIELDS}
        return {
            "poi_id": str(r.get("poi_id") or r.get("fclt_id") or r.get("id") or ""),
            "type": "tour",
            "name": r.get("name") or r.get("fclt_name"),
            "addr": addr,
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "facilities": fac,
            "entrance": r.get("entrance"),
        }

    def search_tour_by_name(self, q: str, sigungu: str = "", limit: int = 10) -> list:
        """이름으로 관광 POI 를 찾는다 (v1.18.0).

        ``list_tour_spots`` 는 ``accessible_facilities`` 가 채워진 행만 본다 — 무장애
        관광지 목록이라는 용도에는 맞지만, 이용자가 말한 목적지를 좌표로 바꾸는
        용도로는 지나치게 좁다. 이름 검색은 그 필터 없이 전체 mv_poi 를 대상으로 하되
        지역(시·군·구) 조건은 그대로 지킨다(#26 의 "안양면" 오탐 방지).
        """
        key = _norm_name(q)
        if len(key) < 2:
            return []
        if self.backend == "none":
            return []
        if self.backend == "file":
            rows = self._load_file("tour_bf.json")
        else:
            variants = sigungu_variants(sigungu)
            params = {"limit": max(limit * 20, 100), "q": "%%%s%%" % q.strip()}
            sg_clause = ""
            if variants:
                ors = []
                for i, v in enumerate(variants):
                    ors.append(
                        "COALESCE(address_road, '') LIKE '%%' || :sg{0} || '%%'"
                        " OR COALESCE(address_detail, '') LIKE '%%' || :sg{0} || '%%'".format(i)
                    )
                    params["sg%d" % i] = v
                sg_clause = "AND (%s)" % " OR ".join(ors)
            rows = self._query(
                """
                SELECT poi_id,
                       title AS name,
                       COALESCE(address_road, address_detail) AS addr,
                       latitude, longitude,
                       detail_json->>'accessible_facilities' AS fac_text,
                       search_filter_json->'search_filter'->>'tourist_type' AS tourist_type
                  FROM mv_poi
                 WHERE language_code = 'ko'
                   AND latitude IS NOT NULL AND longitude IS NOT NULL
                   AND title ILIKE :q
                   {sg}
                 LIMIT :limit
                """.format(sg=sg_clause),
                params,
            )
        out = []
        for r in rows:
            item = self._normalize_tour(r)
            if item["lat"] is None:
                continue
            rank = _name_match_rank(key, item.get("name"))
            if rank is None:
                continue
            item["match_rank"] = rank
            out.append(item)
        out.sort(key=lambda x: (x["match_rank"], len(x.get("name") or ""), x.get("name") or ""))
        return out[:limit]

    def search_stations_by_name(self, q: str, limit: int = 5) -> list:
        """이름으로 지하철역을 찾는다 — "안양역", "범계" 모두 매칭."""
        key = _norm_name(q)
        if len(key) < 1:
            return []
        key = key[:-1] if key.endswith("역") and len(key) > 1 else key
        out = []
        for st in self._stations():
            if st["lat"] is None:
                continue
            rank = _name_match_rank(key, st.get("name"))
            if rank is None:
                continue
            item = dict(st)
            item.update({"type": "transit_station", "match_rank": rank})
            out.append(item)
        out.sort(key=lambda x: (x["match_rank"], x.get("name") or ""))
        return out[:limit]

    def get_tour_spot(self, poi_id: str):
        """ID 우선, 실패하면 이름으로도 찾는다.

        12번 음성 도구는 사용자가 말한 관광지 '이름'을 그대로 넘기는 경우가 있어
        ID 전용 조회는 404 를 낸다. 이름 폴백으로 실사용 실패를 막는다.
        """
        key = str(poi_id).strip()
        rows = self.list_tour_spots(limit=10000)
        for r in rows:
            if r["poi_id"] == key:
                return r
        for r in rows:
            if (r.get("name") or "").strip() == key:
                return r
        norm = key.replace(" ", "")
        for r in rows:
            if (r.get("name") or "").replace(" ", "") == norm:
                return r
        return None

    def get_entrance(self, poi_id: str):
        """무장애 출입구 좌표. 없으면 시설 대표 좌표로 대체(fallback 표기)."""
        spot = self.get_tour_spot(poi_id)
        if spot is None:
            return None
        ent = spot.get("entrance")
        if ent and ent.get("lat") is not None:
            return {"lat": float(ent["lat"]), "lng": float(ent["lng"]),
                    "source": "accessible_entrance"}
        if spot["lat"] is None:
            return None
        return {"lat": spot["lat"], "lng": spot["lng"], "source": "facility_centroid"}

    def recommend_tour(self, disabilities: list, sigungu: str = "안양",
                       match_mode: str = "all", topk: int = 10,
                       origin_lat: float = None, origin_lng: float = None,
                       offset: int = 0) -> list:
        """장애 유형별 무장애 관광지 랭킹.

        10-TripSense 의 filter_and_rank 와 동일한 판단 기준(요구 편의시설 충족 여부)을
        DB 필드 기준으로 옮긴 것. all=모든 유형 충족, any=한 유형이라도 충족.
        """
        required = []
        for d in disabilities or []:
            required.append(set(DISABILITY_REQUIREMENTS.get(d, [])))
        spots = self.list_tour_spots(sigungu=sigungu, limit=10000)

        scored = []
        for s in spots:
            fac = s["facilities"]
            if required:
                per_type = []
                for req in required:
                    if not req:
                        per_type.append(0.0)
                        continue
                    hit = sum(1 for f in req if fac.get(f))
                    per_type.append(hit / len(req))
                ok = all(p > 0 for p in per_type) if match_mode == "all" else any(p > 0 for p in per_type)
                if not ok:
                    continue
                score = sum(per_type) / len(per_type)
            else:
                score = sum(1 for f in TOUR_FIELDS if fac.get(f)) / len(TOUR_FIELDS)
            item = dict(s)
            item["score"] = round(score, 3)
            item["matched"] = [f for f in TOUR_FIELDS if fac.get(f)]
            scored.append(item)

        # 출발지가 주어지면 거리 오름차순 — 반경 제한 없이 전체를 이어서 페이징한다.
        # (offset 이 있어도 정렬 기준은 동일하므로 목록 순서가 뒤섞이지 않는다)
        if origin_lat is not None and origin_lng is not None:
            for it in scored:
                if it["lat"] is None or it["lng"] is None:
                    it["distance_m"] = None
                    continue
                it["distance_m"] = round(
                    haversine_m(origin_lat, origin_lng, it["lat"], it["lng"]), 1
                )
            scored.sort(key=lambda x: (x["distance_m"] is None,
                                       x["distance_m"] if x["distance_m"] is not None else 0,
                                       x["name"] or ""))
        else:
            scored.sort(key=lambda x: (-x["score"], x["name"] or ""))

        start = max(0, int(offset or 0))
        return scored[start:start + topk]

    # ---------- 대중교통 접근점 ----------
    def list_transit_access(self, lat: float, lng: float, radius_m: float = 800,
                            profile_id: str = "wheelchair_manual", limit: int = 20) -> list:
        """휠체어로 접근 가능한 정류장·역.

        - 지하철역: 승강설비 보유 여부로 접근 가능/경고 판정
        - 버스정류장: 저상버스 정차 여부는 정적 DB 에 없다 -> 실시간 도착정보(GBIS lowPlate)
          로 프론트/클라이언트가 확인해야 한다. 여기서는 위치·노선 정보만 제공한다.
        """
        stations = self._stations()
        stops = self._stops(lat=lat, lng=lng, radius_m=radius_m)
        needs_elevator = profile_id.startswith("wheelchair")

        out = []
        for s in stations:
            if s["lat"] is None:
                continue
            d = haversine_m(lat, lng, s["lat"], s["lng"])
            if d > radius_m:
                continue
            warnings = []
            accessible = True
            if needs_elevator:
                if not s.get("elevator_cnt"):
                    accessible = bool(s.get("wheelchair_lift_cnt"))
                    warnings.append(
                        "엘리베이터가 없습니다. 휠체어리프트만 있어 이용에 도움이 필요할 수 있습니다."
                        if s.get("wheelchair_lift_cnt")
                        else "엘리베이터·리프트 정보가 없어 휠체어 이용이 어려울 수 있습니다."
                    )
            item = dict(s)
            item.update({"type": "transit_station", "dist_m": round(d),
                         "accessible": accessible,
                         "accessible_status": "yes" if accessible else "no",
                         "warnings": warnings})
            out.append(item)

        for s in stops:
            if s.get("lat") is None:
                continue
            d = haversine_m(lat, lng, s["lat"], s["lng"])
            if d > radius_m:
                continue
            warnings = ["저상버스 정차 여부가 확인되지 않았습니다. "
                        "실시간 도착정보로 확인하세요."]
            if s.get("center_yn"):
                # 중앙차로 정류소는 승차장이 도로 한가운데 있어 횡단이 선행된다.
                warnings.append("중앙차로 정류소입니다. 승차장까지 횡단이 필요합니다.")
            item = dict(s)
            item.update({
                "type": "transit_stop",
                "dist_m": round(d),
                # 저상버스 정차 여부를 알 수 없어 접근 가능으로 단정하지 않는다.
                # 역은 승강설비로 판정하지만 정류장은 판정 근거가 없어 None(미판정)이다.
                # None 은 "접근 불가"가 아니다. 소비 측이 falsy 로 뭉뚱그려 불가로
                # 표시하지 않도록, 판정 상태를 accessible_status 로 따로 준다.
                "accessible": None,
                "accessible_status": "unknown",
                "warnings": warnings,
            })
            out.append(item)

        out.sort(key=lambda x: x["dist_m"])
        return out[:limit]

    def _stations(self) -> list:
        if self.backend == "none":
            return []
        if self.backend == "file":
            rows = self._load_file("stations.json")
        else:
            rows = self._query(
                """
                SELECT s.stn_cd AS poi_id, s.stn_name AS name, s.line_name, s.latitude, s.longitude,
                       s.elevator_cnt, s.wheelchair_lift_cnt, s.dis_slope_yn,
                       -- 코레일 API 가 응답하지 않는 역(NULL)은 설비 단위 화장실 자료로 보완한다
                       COALESCE(s.dis_toilet_yn,
                                CASE WHEN EXISTS (SELECT 1 FROM poi_station_toilet_unit t
                                                   WHERE t.stn_name = s.stn_name AND t.disabled_yn = 'Y'
                                                     AND COALESCE(t.del_yn, 'N') = 'N'
                                                     AND (s.line_name IS NULL OR t.line_name = s.line_name))
                                     THEN 'Y' END) AS dis_toilet_yn
                  FROM poi_station_access_status s
                 WHERE s.del_yn = 'N' AND s.anyang_yn = 'Y'
                """,
                {},
            )
        norm = []
        for r in rows:
            lat = r.get("latitude", r.get("lat"))
            lng = r.get("longitude", r.get("lng"))
            norm.append({
                "poi_id": str(r.get("poi_id") or r.get("stn_cd") or ""),
                "name": r.get("name") or r.get("stn_name"),
                "lat": float(lat) if lat is not None else None,
                "lng": float(lng) if lng is not None else None,
                "elevator_cnt": int(r.get("elevator_cnt") or 0),
                "wheelchair_lift_cnt": int(r.get("wheelchair_lift_cnt") or 0),
                "dis_toilet_yn": _is_y(r.get("dis_toilet_yn")),
                # NULL(자료 없음)과 N(없음)은 다르다 — 6역은 코레일 API 가 응답하지 않아 NULL 이다
                "dis_toilet_status": _yn3(r.get("dis_toilet_yn")),
                "dis_slope_status": _yn3(r.get("dis_slope_yn")),
                "line": r.get("line_name") or r.get("line"),
            })
        return norm

    def _stops(self, lat=None, lng=None, radius_m=None, poi_id: str = "") -> list:
        """버스 정류장.

        db 백엔드는 01 의 ``tran_bus_station_info`` / ``tran_bus_route_station`` 을 읽는다
        (08 이 GBIS 경유정류소 목록조회로 적재). 안양 연관 노선의 전 경유지가 들어와
        전국 범위가 되므로, 반경이 주어지면 **bbox 로 먼저 좁혀** 전수 로드를 피한다.

        저상버스 정차 여부는 정적 DB 에 없다 — 실시간 도착정보(GBIS lowPlate)로
        클라이언트가 확인한다. 여기서는 위치·정류소번호·경유노선·중앙차로 여부만 제공한다.
        """
        if self.backend == "none":
            return []
        if self.backend == "file":
            rows = self._load_file("transit_stops.json")
        else:
            where = ["COALESCE(s.del_yn, 'N') = 'N'",
                     "s.latitude IS NOT NULL", "s.longitude IS NOT NULL"]
            params = {}
            if poi_id:
                where.append("s.station_id::text = :poi_id")
                params["poi_id"] = str(poi_id)
            elif lat is not None and lng is not None and radius_m:
                d_lat = float(radius_m) / 111320.0
                d_lng = d_lat / max(math.cos(math.radians(lat)), 0.01)
                where.append("s.latitude BETWEEN :min_lat AND :max_lat")
                where.append("s.longitude BETWEEN :min_lng AND :max_lng")
                params.update({"min_lat": lat - d_lat, "max_lat": lat + d_lat,
                               "min_lng": lng - d_lng, "max_lng": lng + d_lng})
            rows = self._query(
                """
                SELECT s.station_id AS poi_id, s.station_name AS name,
                       s.latitude, s.longitude, s.mobile_no, s.center_yn,
                       (SELECT json_agg(json_build_object(
                                   'route_id', t.route_id,
                                   'name', t.route_name,
                                   'type', t.route_type_name,
                                   'end_station', t.end_station_name,
                                   'station_seq', t.seqs)
                                 ORDER BY t.route_name, t.route_id)
                          FROM (SELECT r.route_id, r.route_name, r.route_type_name,
                                       r.end_station_name,
                                       array_agg(rs.station_seq
                                                 ORDER BY rs.station_seq) AS seqs
                                  FROM tran_bus_route_station rs
                                  JOIN tran_bus_route_info r
                                    ON r.route_id = rs.route_id
                                 WHERE rs.station_id = s.station_id
                                   AND COALESCE(rs.del_yn, 'N') = 'N'
                                   AND COALESCE(r.del_yn, 'N') = 'N'
                                 GROUP BY r.route_id, r.route_name,
                                          r.route_type_name,
                                          r.end_station_name) t) AS route_list
                  FROM tran_bus_station_info s
                 WHERE """ + " AND ".join(where),
                params,
            )
        norm = []
        for r in rows:
            lat_v = r.get("lat", r.get("latitude"))
            lng_v = r.get("lng", r.get("longitude"))
            routes = _normalize_routes(r.get("routes"), r.get("route_list"))
            mobile_no = r.get("mobile_no")
            norm.append({
                "poi_id": str(r.get("poi_id") or r.get("station_id") or ""),
                "name": r.get("name") or r.get("station_name"),
                "lat": float(lat_v) if lat_v is not None else None,
                "lng": float(lng_v) if lng_v is not None else None,
                "mobile_no": (str(mobile_no).strip() or None) if mobile_no else None,
                "center_yn": _is_y(r.get("center_yn")),
                "routes": routes,
            })
        return norm

    # ---------- 멀티모달 플래너 지원 (#36) ----------
    def stops_near(self, lat: float, lng: float, radius_m: float) -> list:
        """반경 내 버스 정류장(경유 노선 포함). bbox 선필터는 _stops 가 수행."""
        return [s for s in self._stops(lat=lat, lng=lng, radius_m=radius_m)
                if s["lat"] is not None]

    def stations(self) -> list:
        """안양 관내 지하철역(승강설비 포함)."""
        return [s for s in self._stations() if s["lat"] is not None]

    def route_stop_path(self, route_id, seq_from: int, seq_to: int) -> list:
        """노선의 구간 경유 정류장(순번 오름차순) — 버스 leg geometry·하차 카운트다운용."""
        if self.backend == "none":
            return []
        if self.backend == "file":
            rows = [r for r in self._load_file("transit_route_paths.json")
                    if str(r.get("route_id")) == str(route_id)
                    and seq_from <= int(r.get("station_seq", -1)) <= seq_to]
            rows.sort(key=lambda r: int(r["station_seq"]))
        else:
            rows = self._query(
                """
                SELECT rs.station_seq, s.station_name AS name, s.mobile_no,
                       s.latitude, s.longitude
                  FROM tran_bus_route_station rs
                  JOIN tran_bus_station_info s ON s.station_id = rs.station_id
                 WHERE rs.route_id = :route_id
                   AND rs.station_seq BETWEEN :seq_from AND :seq_to
                   AND COALESCE(rs.del_yn, 'N') = 'N'
                   AND COALESCE(s.del_yn, 'N') = 'N'
                 ORDER BY rs.station_seq
                """,
                {"route_id": route_id, "seq_from": seq_from, "seq_to": seq_to},
            )
        out = []
        for r in rows:
            lat_v = r.get("lat", r.get("latitude"))
            lng_v = r.get("lng", r.get("longitude"))
            if lat_v is None:
                continue
            mob = r.get("mobile_no")
            out.append({
                "station_seq": int(r["station_seq"]),
                "name": r.get("name"),
                "mobile_no": (str(mob).strip() or None) if mob else None,
                "lat": float(lat_v), "lng": float(lng_v),
            })
        return out

    # ---------- 역 편의시설 (설비 단위, v1.19.0) ----------
    _FACILITY_UNIT_TABLES = {
        # 키: (테이블, 정렬 컬럼, 반환 컬럼)
        "elevators": ("poi_station_elevator_unit", "unit_seq",
                      "exit_no, detail_loc, capacity_person, capacity_kg"),
        "lifts": ("poi_station_wheelchair_lift", "mng_no",
                  "mng_no, exit_no, detail_loc, length_mm, width_mm, start_floor, end_floor"),
        "toilets": ("poi_station_toilet_unit", "unit_seq",
                    "gate_inout, exit_no, detail_loc, toilet_kind, disabled_yn, floor_no, ground_dv"),
        "platforms": ("poi_station_platform", "platform_no",
                      "platform_no, updown, ground_dv, floor_no, platform_connect_yn, "
                      "screen_door_yn, safety_plate_yn, gap_min_cm, gap_max_cm, gap_avg_cm, door_cnt"),
    }

    def station_facilities(self, stn_cd: str = "", name: str = ""):
        """역 하나의 편의시설 — 개수·유무(poi_station_access_status)에 설비 단위
        상세(엘리베이터·리프트·화장실·승강장)를 붙인다.

        개수만으로는 "어느 출입구 엘리베이터를 타라"를 말할 수 없다. 설비 단위 자료가
        DB 에 없으면 목록이 비어 오고, 그때는 개수·유무만으로 안내한다.
        유무 필드는 3상태(yes/no/unknown)다 — 코레일 API 가 응답하지 않는 역은 NULL 이라
        "없음"으로 말하면 틀린다.
        """
        if self.backend == "none":
            return None
        key = _norm_name(name or "")
        key = key[:-1] if key.endswith("역") and len(key) > 1 else key
        if self.backend == "file":
            for r in self._load_file("station_facilities.json"):
                if (stn_cd and str(r.get("stn_cd")) == str(stn_cd)) or \
                        (key and _norm_name(r.get("name") or r.get("stn_name") or "") == key):
                    return self._norm_facilities(r)
            return None
        params = {"cd": stn_cd or "", "name": key or ""}
        rows = self._query(
            """
            SELECT stn_cd, stn_name, line_name, latitude, longitude, anyang_yn, base_dt,
                   elevator_cnt, escalator_cnt, wheelchair_lift_cnt,
                   dis_slope_yn, dis_toilet_yn, gen_toilet_yn, nursing_room_yn, info_center_yn
              FROM poi_station_access_status
             WHERE del_yn = 'N'
               AND ((:cd <> '' AND stn_cd = :cd) OR (:name <> '' AND stn_name = :name))
             ORDER BY anyang_yn DESC, stn_cd
             LIMIT 1
            """, params)
        if not rows:
            return None
        base = dict(rows[0])
        line = base.get("line_name")
        for k, (table, order_col, cols) in self._FACILITY_UNIT_TABLES.items():
            sql = ("SELECT %s FROM %s WHERE del_yn = 'N' AND stn_name = :name"
                   % (cols, table))
            p = {"name": base["stn_name"]}
            if line:
                sql += " AND line_name = :line"
                p["line"] = line
            sql += " ORDER BY %s" % order_col
            try:
                base[k] = self._query(sql, p)
            except Exception as e:          # 테이블 미생성(적재 전) — 목록만 비운다
                logger.warning("역 설비 조회 실패 %s: %s", table, e)
                base[k] = []
        return self._norm_facilities(base)

    @staticmethod
    def _norm_facilities(r: dict) -> dict:
        def _i(v):
            return None if v is None or v == "" else int(v)

        def _f(v):
            return None if v is None or v == "" else round(float(v), 1)

        def _s(v):
            return None if v is None else (str(v).strip() or None)

        elevators = [{"exit_no": _s(e.get("exit_no")), "detail_loc": _s(e.get("detail_loc")),
                      "capacity_person": _i(e.get("capacity_person")),
                      "capacity_kg": _i(e.get("capacity_kg"))}
                     for e in (r.get("elevators") or [])]
        lifts = [{"mng_no": _s(l.get("mng_no")), "exit_no": _s(l.get("exit_no")),
                  "detail_loc": _s(l.get("detail_loc")),
                  "length_mm": _i(l.get("length_mm")), "width_mm": _i(l.get("width_mm")),
                  "start_floor": _s(l.get("start_floor")), "end_floor": _s(l.get("end_floor"))}
                 for l in (r.get("lifts") or [])]
        toilets = [{"gate_inout": _s(t.get("gate_inout")), "exit_no": _s(t.get("exit_no")),
                    "detail_loc": _s(t.get("detail_loc")), "kind": _s(t.get("toilet_kind") or t.get("kind")),
                    "disabled": _is_y(t.get("disabled_yn", t.get("disabled"))),
                    "floor": _s(t.get("floor_no") or t.get("floor")),
                    "ground": _s(t.get("ground_dv") or t.get("ground"))}
                   for t in (r.get("toilets") or [])]
        platforms = [{"platform_no": _s(p.get("platform_no")), "updown": _s(p.get("updown")),
                      "ground": _s(p.get("ground_dv") or p.get("ground")),
                      "floor": _s(p.get("floor_no") or p.get("floor")),
                      "platform_connect": _yn3(p.get("platform_connect_yn")),
                      "screen_door": _yn3(p.get("screen_door_yn")),
                      "safety_plate": _yn3(p.get("safety_plate_yn")),
                      "gap_min_cm": _f(p.get("gap_min_cm")), "gap_max_cm": _f(p.get("gap_max_cm")),
                      "gap_avg_cm": _f(p.get("gap_avg_cm")), "door_cnt": _i(p.get("door_cnt"))}
                     for p in (r.get("platforms") or [])]
        lat = r.get("latitude", r.get("lat"))
        lng = r.get("longitude", r.get("lng"))
        ev_cnt = _i(r.get("elevator_cnt"))
        lift_cnt = _i(r.get("wheelchair_lift_cnt"))
        return {
            "poi_id": str(r.get("stn_cd") or r.get("poi_id") or ""),
            "name": r.get("stn_name") or r.get("name"),
            "line": r.get("line_name") or r.get("line"),
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "anyang": _is_y(r.get("anyang_yn", "Y")),
            "base_dt": str(r.get("base_dt")) if r.get("base_dt") else None,
            "counts": {
                "elevator": ev_cnt if ev_cnt is not None else len(elevators) or None,
                "escalator": _i(r.get("escalator_cnt")),
                "wheelchair_lift": lift_cnt if lift_cnt is not None else len(lifts) or None,
            },
            "status": {
                "dis_slope": _yn3(r.get("dis_slope_yn")),
                "dis_toilet": ("yes" if any(t["disabled"] for t in toilets)
                               else _yn3(r.get("dis_toilet_yn"))),
                "gen_toilet": ("yes" if any(not t["disabled"] for t in toilets)
                               else _yn3(r.get("gen_toilet_yn"))),
                "nursing_room": _yn3(r.get("nursing_room_yn")),
                "info_center": _yn3(r.get("info_center_yn")),
                "safety_plate": ("yes" if any(p["safety_plate"] == "yes" for p in platforms)
                                 else ("no" if platforms and all(p["safety_plate"] == "no" for p in platforms)
                                       else "unknown")),
            },
            "elevators": elevators,
            "lifts": lifts,
            "toilets": toilets,
            "platforms": platforms,
        }

    def stop_route_meta(self, station_id) -> dict:
        """정류장을 지나는 노선의 정적 메타 {route_id: {name,type,end_station}} —
        실시간 도착정보에 노선 유형·종점명을 덧입히는 데 쓴다."""
        out = {}
        for s in self._stops(poi_id=str(station_id)):
            for r in s.get("routes") or []:
                if isinstance(r, dict) and r.get("route_id") is not None:
                    out[str(r["route_id"])] = {"name": r.get("name"), "type": r.get("type"),
                                               "end_station": r.get("end_station")}
        return out

    def route_stops(self, route_id) -> list:
        """노선의 전 경유 정류장(순번 오름차순, station_id 포함) — 차량 위치를 정류장에 붙인다."""
        if self.backend == "none":
            return []
        if self.backend == "file":
            rows = [r for r in self._load_file("transit_route_paths.json")
                    if str(r.get("route_id")) == str(route_id)]
            rows.sort(key=lambda r: int(r.get("station_seq", 0)))
        else:
            rows = self._query(
                """
                SELECT rs.station_seq, rs.station_id, s.station_name AS name, s.mobile_no,
                       s.latitude, s.longitude
                  FROM tran_bus_route_station rs
                  JOIN tran_bus_station_info s ON s.station_id = rs.station_id
                 WHERE rs.route_id = :route_id
                   AND COALESCE(rs.del_yn, 'N') = 'N'
                   AND COALESCE(s.del_yn, 'N') = 'N'
                 ORDER BY rs.station_seq
                """, {"route_id": route_id})
        out = []
        for r in rows:
            lat_v = r.get("lat", r.get("latitude"))
            lng_v = r.get("lng", r.get("longitude"))
            mob = r.get("mobile_no")
            out.append({
                "station_id": str(r.get("station_id") or r.get("poi_id") or ""),
                "station_seq": int(r.get("station_seq") or 0),
                "name": r.get("name"),
                "mobile_no": (str(mob).strip() or None) if mob else None,
                "lat": float(lat_v) if lat_v is not None else None,
                "lng": float(lng_v) if lng_v is not None else None,
            })
        return out

    def resolve_destination(self, dest_type: str, poi_id: str):
        """목적지 유형별 좌표 해석. tour 는 무장애 출입구 우선."""
        if dest_type == "tour":
            return self.get_entrance(poi_id)
        pool = (self._stations() if dest_type == "transit_station"
                else self._stops(poi_id=poi_id))
        for s in pool:
            if s["poi_id"] == str(poi_id) and s["lat"] is not None:
                return {"lat": s["lat"], "lng": s["lng"], "source": dest_type}
        return None


STORE = PoiStore()


def configure(settings) -> PoiStore:
    global STORE
    STORE = PoiStore(
        backend=settings.poi_backend,
        data_dir=settings.poi_data_dir,
        dsn=settings.poi_db_dsn,
    )
    return STORE
