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
import math
import os

from ..engine.geo import haversine_m

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


def _is_y(v) -> bool:
    return str(v).strip().upper() in ("Y", "TRUE", "1")


# 시·군·구 행정단위 접미사. "안양" 처럼 접미사 없이 들어온 지역명은 이 접미사를 붙인
# 변형("안양시"/"안양군"/"안양구")으로만 주소를 대조한다. 단순 부분일치는 전남 장흥군
# "안양면" 같은 타지역 주소까지 끌어온다(#26).
_SIGUNGU_SUFFIXES = ("시", "군", "구")


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
                         "accessible": accessible, "warnings": warnings})
            out.append(item)

        for s in stops:
            if s.get("lat") is None:
                continue
            d = haversine_m(lat, lng, s["lat"], s["lng"])
            if d > radius_m:
                continue
            warnings = ["저상버스 여부는 실시간 도착정보로 확인하세요."]
            if s.get("center_yn"):
                # 중앙차로 정류소는 승차장이 도로 한가운데 있어 횡단이 선행된다.
                warnings.append("중앙차로 정류소입니다. 승차장까지 횡단이 필요합니다.")
            item = dict(s)
            item.update({
                "type": "transit_stop",
                "dist_m": round(d),
                "accessible": True,
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
                SELECT stn_cd AS poi_id, stn_name AS name, latitude, longitude,
                       elevator_cnt, wheelchair_lift_cnt, dis_toilet_yn, dis_slope_yn
                  FROM poi_station_access_status
                 WHERE del_yn = 'N' AND anyang_yn = 'Y'
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
                       (SELECT string_agg(DISTINCT r.route_name, ',')
                          FROM tran_bus_route_station rs
                          JOIN tran_bus_route_info r ON r.route_id = rs.route_id
                         WHERE rs.station_id = s.station_id
                           AND COALESCE(rs.del_yn, 'N') = 'N'
                           AND COALESCE(r.del_yn, 'N') = 'N') AS route_names
                  FROM tran_bus_station_info s
                 WHERE """ + " AND ".join(where),
                params,
            )
        norm = []
        for r in rows:
            lat_v = r.get("lat", r.get("latitude"))
            lng_v = r.get("lng", r.get("longitude"))
            routes = r.get("routes")
            if routes is None:
                raw = str(r.get("route_names") or "")
                routes = sorted({x for x in raw.split(",") if x})
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
