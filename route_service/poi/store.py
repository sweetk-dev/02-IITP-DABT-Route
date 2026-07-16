# -*- coding: utf-8 -*-
"""POI 조회 — 무장애 관광지 / 대중교통 접근점.

백엔드 3종 (POI_BACKEND 환경변수)
  db   : 01-IITP-DABT-Database 읽기 전용 조회
         poi_tour_bf_facility / poi_station_access_status /
         poi_station_wheelchair_lift / poi_facility_accessibility
  file : POI_DATA_DIR 아래 JSON (오프라인·개발·데모용)
  none : 빈 결과 (source="none") — 적재 전에도 서비스가 기동되도록

⚠️ 데이터 적재 현황: 이동편의 데이터 파이프라인(01 테이블 + 08 어댑터)이 적재 완료되기
전까지 db 백엔드는 빈 결과를 낼 수 있다. 응답의 source 필드로 클라이언트가 구분한다.
"""
from __future__ import annotations

import json
import os

from ..engine.geo import haversine_m

# 01-IITP-DABT-Database `poi_tour_bf_facility` 실제 컬럼 (2026-07-13 실측)
TOUR_FIELDS = [
    "toilet_yn", "elevator_yn", "parking_yn", "slope_yn", "subway_yn", "bus_stop_yn",
    "wheelchair_rent_yn", "tactile_map_yn", "audio_guide_yn", "nursing_room_yn",
    "accessible_room_yn", "stroller_rent_yn",
]

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
        if self.backend == "file":
            rows = self._load_file("tour_bf.json")
        else:
            rows = self._query(
                """
                SELECT fclt_id AS poi_id,
                       fclt_name AS name,
                       COALESCE(addr_road, addr_jibun) AS addr,
                       latitude, longitude,
                       toilet_yn, elevator_yn, parking_yn, slope_yn,
                       subway_yn, bus_stop_yn, wheelchair_rent_yn, tactile_map_yn,
                       audio_guide_yn, nursing_room_yn, accessible_room_yn, stroller_rent_yn
                  FROM poi_tour_bf_facility
                 WHERE del_yn = 'N'
                   AND (:sigungu = ''
                        OR COALESCE(addr_road, '') LIKE '%%' || :sigungu || '%%'
                        OR COALESCE(addr_jibun, '') LIKE '%%' || :sigungu || '%%'
                        OR fclt_name LIKE '%%' || :sigungu || '%%')
                 LIMIT :limit
                """,
                {"sigungu": sigungu or "", "limit": limit},
            )
        out = [self._normalize_tour(r) for r in rows]
        if sigungu and self.backend == "file":
            out = [
                r for r in out
                if sigungu in (r.get("addr") or "") or sigungu in (r.get("name") or "")
            ]
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
    def _normalize_tour(r: dict) -> dict:
        lat = r.get("latitude", r.get("lat"))
        lng = r.get("longitude", r.get("lng"))
        addr = r.get("addr") or r.get("addr_road") or r.get("addr_jibun")
        return {
            "poi_id": str(r.get("poi_id") or r.get("fclt_id") or r.get("id") or ""),
            "type": "tour",
            "name": r.get("name") or r.get("fclt_name"),
            "addr": addr,
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "facilities": {k: _is_y(r.get(k)) for k in TOUR_FIELDS},
            "entrance": r.get("entrance"),
        }

    def get_tour_spot(self, poi_id: str):
        for r in self.list_tour_spots(limit=10000):
            if r["poi_id"] == str(poi_id):
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
                       match_mode: str = "all", topk: int = 10) -> list:
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

        scored.sort(key=lambda x: (-x["score"], x["name"] or ""))
        return scored[:topk]

    # ---------- 대중교통 접근점 ----------
    def list_transit_access(self, lat: float, lng: float, radius_m: float = 800,
                            profile_id: str = "wheelchair_manual", limit: int = 20) -> list:
        """휠체어로 접근 가능한 정류장·역.

        - 지하철역: 승강설비 보유 여부로 접근 가능/경고 판정
        - 버스정류장: 저상버스 정차 여부는 정적 DB 에 없다 -> 실시간 도착정보(GBIS lowPlate)
          로 프론트/클라이언트가 확인해야 한다. 여기서는 위치·노선 정보만 제공한다.
        """
        stations = self._stations()
        stops = self._stops()
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
            item = dict(s)
            item.update({
                "type": "transit_stop",
                "dist_m": round(d),
                "accessible": True,
                "warnings": ["저상버스 여부는 실시간 도착정보로 확인하세요."],
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

    def _stops(self) -> list:
        """버스 정류장 — 현재 01 DB 에 정류장 좌표 테이블이 없어 file 백엔드만 지원."""
        if self.backend != "file":
            return []
        rows = self._load_file("transit_stops.json")
        norm = []
        for r in rows:
            lat = r.get("lat", r.get("latitude"))
            lng = r.get("lng", r.get("longitude"))
            norm.append({
                "poi_id": str(r.get("poi_id") or r.get("station_id") or ""),
                "name": r.get("name") or r.get("station_name"),
                "lat": float(lat) if lat is not None else None,
                "lng": float(lng) if lng is not None else None,
                "routes": r.get("routes") or [],
            })
        return norm

    def resolve_destination(self, dest_type: str, poi_id: str):
        """목적지 유형별 좌표 해석. tour 는 무장애 출입구 우선."""
        if dest_type == "tour":
            return self.get_entrance(poi_id)
        pool = self._stations() if dest_type == "transit_station" else self._stops()
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
