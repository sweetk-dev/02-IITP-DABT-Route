# -*- coding: utf-8 -*-
"""목적지 접근 지점(무장애 출입구) 해석.

문제: POI 좌표는 시설 **대표점(건물 중심)** 이다. 그대로 경로 목적지로 쓰면 보행망의
건물 뒤편 도로에 스냅되어 "도착했습니다" 라고 안내한 지점에서 실제 출입구까지
휠체어로 건물을 한 바퀴 돌아야 하는 상황이 생긴다. 현장 실증에서 바로 문제가 된다.

데이터 실측(2026-07-13, 안양):
  - OSM `entrance` 노드: 안양 전역 86개, `wheelchair` 태그 0개,
    무장애 관광지 13곳 중 50m 이내 매칭 **0곳** -> 사용 불가
  - OSM 건물 폴리곤: 9,561개 -> 사용 가능

그래서 3단계로 해석한다(정확한 것부터).

  1. manual   : 현장 실측 출입구 좌표 (data/poi/entrances.json). 연구진 답사 결과를 넣는다.
  2. building : POI 를 포함하는 건물 폴리곤의 **경계점 중 보행망에 가장 가까운 지점**.
                실제 출입구는 아니지만 건물 중심보다 항상 낫고 결정적이다.
                프로필상 통행 가능한 링크만 후보로 삼는다(계단으로만 닿는 면은 배제).
  3. centroid : 위 둘이 없으면 시설 대표점. 응답에 그대로 표기해 사용자가 알 수 있게 한다.
"""
from __future__ import annotations

import json
import logging
import math
import os
import pickle
import re

from .geo import haversine_m
from .snap import snap

logger = logging.getLogger("route_access")


class BuildingIndex:
    """건물 폴리곤 인덱스 (scripts/build_network.py --buildings 로 생성)."""

    def __init__(self, path: str = ""):
        self.polys = []          # [(shapely Polygon, name)]
        self.loaded = False
        self._names = None       # 이름 검색 인덱스 (지연 생성)
        if path and os.path.exists(path):
            try:
                self.load(path)
            except Exception as e:  # shapely 미설치·파일 손상 등
                # 접근점 해석은 부가 기능이다. 실패해도 경로 안내 자체는 계속돼야 하므로
                # 서비스를 죽이지 않고 비활성화한다(목적지는 시설 대표점으로 해석).
                logger.warning("건물 폴리곤 로드 실패 — 접근점 해석 비활성화 (%s)", e)

    def load(self, path: str):
        with open(path, "rb") as f:
            self.polys = pickle.load(f)
        self.loaded = True
        return len(self.polys)

    # ── 이름으로 장소 찾기 (v1.18.0) ──
    # 건물 폴리곤에는 OSM 이름이 함께 들어 있다(안양 9,563동 중 2,032동이 이름 보유).
    # 관광지·역이 아닌 일반 시설 — 시청·구청·복지관·도서관·학교·병원 — 은 이 이름이
    # 유일한 좌표 출처다. 이 인덱스가 없으면 이용자가 말한 목적지를 좌표로 바꿀 방법이
    # 없어 "서비스 지역 밖"으로 잘못 안내된다(12번 실사용 결함).
    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[\s\-_·.,()]+", "", str(s or ""))

    def _name_index(self) -> list:
        """[(name, norm_name, lat, lng)] — 대표점은 폴리곤 내부가 보장되는 점을 쓴다."""
        if self._names is not None:
            return self._names
        idx = []
        if self.loaded:
            seen = set()
            for poly, name in self.polys:
                if not name or not str(name).strip():
                    continue
                try:
                    pt = poly.representative_point()
                except Exception:
                    continue
                lat, lng = round(pt.y, 7), round(pt.x, 7)
                key = (self._norm(name), round(lat, 4), round(lng, 4))
                if key in seen:          # 같은 시설의 분할 폴리곤 중복 제거
                    continue
                seen.add(key)
                idx.append((str(name).strip(), self._norm(name), lat, lng))
        self._names = idx
        return idx

    def search_by_name(self, q: str, limit: int = 8) -> list:
        """이름으로 건물을 찾는다 — 완전일치 > 접두 > 부분일치 순.

        질의가 이름을 포함하는 역방향 일치("안양시청 민원실" -> "안양시청")도 허용하되,
        두 글자 이하 이름은 오탐이 많아(예: "역") 역방향 대상에서 제외한다.
        """
        nq = self._norm(q)
        if len(nq) < 2:
            return []
        hits = []
        for name, nname, lat, lng in self._name_index():
            if nname == nq:
                rank = 0
            elif nname.startswith(nq):
                rank = 1
            elif nq in nname:
                rank = 2
            elif len(nname) >= 3 and nname in nq:
                rank = 3
            else:
                continue
            hits.append((rank, len(nname), name, lat, lng))
        hits.sort(key=lambda x: (x[0], x[1], x[2]))
        return [{"type": "building", "poi_id": None, "name": nm,
                 "lat": la, "lng": ln, "match_rank": rk}
                for rk, _l, nm, la, ln in hits[:limit]]

    def containing(self, lat: float, lng: float, near_m: float = 40.0):
        """점을 포함하는 건물. 없으면 near_m 이내 최근접 건물.

        POI 대표점이 건물 폴리곤 살짝 밖(주차장·마당 쪽)에 찍힌 사례가 많아
        25m 로는 놓치는 시설이 있었다(#27, resolved_by=facility_centroid).
        40m 로 완화 — 시설 부지 규모에서 엉뚱한 옆 건물로 붙을 위험은
        '최근접' 선택이 흡수한다.
        """
        if not self.loaded:
            return None
        try:
            from shapely.geometry import Point
        except ImportError:
            return None

        p = Point(lng, lat)
        best, best_d = None, float("inf")
        for poly, _name in self.polys:
            if poly.contains(p):
                return poly
            d = poly.distance(p)           # 도 단위 근사 — 후보 좁히기 용도
            if d < best_d:
                best, best_d = poly, d
        if best is None:
            return None
        # 도 -> m 근사 (위도 37도 기준)
        if best_d * 88000 <= near_m:
            return best
        return None


def _boundary_samples(poly, step_m: float = 5.0):
    """건물 외곽선을 step_m 간격 점열로."""
    ring = poly.exterior
    length_deg = ring.length
    if length_deg <= 0:
        return []
    # 도 -> m 근사(위도 37도): 1도 ≈ 88km(경도) / 111km(위도) -> 평균 100km 로 잡음
    length_m = length_deg * 100000.0
    n = max(int(length_m // step_m), 8)
    n = min(n, 400)                        # 과도한 샘플 방지
    pts = [ring.interpolate(i / n, normalized=True) for i in range(n)]
    return [(pt.y, pt.x) for pt in pts]    # (lat, lng)


def resolve_access_point(store, lat: float, lng: float, profile,
                         buildings: BuildingIndex = None,
                         max_walk_m: float = 120.0, allowed=None) -> dict:
    """목적지 좌표 -> 접근 지점.

    반환: {"lat","lng","source","snap_dist_m"}
    """
    if buildings is None or not buildings.loaded:
        return {"lat": lat, "lng": lng, "source": "facility_centroid"}

    poly = buildings.containing(lat, lng)
    if poly is None:
        return {"lat": lat, "lng": lng, "source": "facility_centroid"}

    best = None
    for blat, blng in _boundary_samples(poly):
        try:
            s = snap(store, blat, blng, profile, max_dist_m=max_walk_m, allowed=allowed)
        except Exception:
            continue
        if not s["reachable"] or not s.get("profile_ok", True):
            continue
        if best is None or s["dist_m"] < best[0]:
            best = (s["dist_m"], blat, blng)

    if best is None:
        return {"lat": lat, "lng": lng, "source": "facility_centroid"}

    dist, blat, blng = best
    return {
        "lat": round(blat, 7),
        "lng": round(blng, 7),
        "source": "building_access",
        "snap_dist_m": round(dist, 1),
        "moved_m": round(haversine_m(lat, lng, blat, blng), 1),
    }


class ManualEntrances:
    """현장 실측 출입구 좌표 (data/poi/entrances.json).

    형식: {"<poi_id>": {"lat": 37.39, "lng": 126.95, "note": "정문 경사로, 2026-09-01 실측"}}
    실증 답사에서 확인한 출입구를 여기에 넣으면 무엇보다 우선한다.
    """

    def __init__(self, path: str = ""):
        self.items = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)

    def get(self, poi_id: str):
        v = self.items.get(str(poi_id))
        if not v or v.get("lat") is None:
            return None
        return {
            "lat": float(v["lat"]),
            "lng": float(v["lng"]),
            "source": "manual_survey",
            "note": v.get("note"),
        }
