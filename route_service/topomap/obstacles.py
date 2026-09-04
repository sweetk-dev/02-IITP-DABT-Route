# -*- coding: utf-8 -*-
"""통행 장애물 색인 — 계단 면형 · 옹벽 · 담장 · 가드펜스 · 수목 (v1.21.0).

두 곳에서 쓴다.
  1) 기존 링크가 계단 면형을 관통하는지 검사 (scripts/mark_stairs_from_topomap.py)
     실측 2026-09-04: 안양 계단 1,317개 중 그래프 링크와 교차하는 것 64개,
     3m 이상 관통하면서 휠체어 통행 가능으로 판정되던 링크 **19개**.
  2) 우회 삼각형 직결 링크 신설의 하드 게이트 (topomap.refine)

좌표계는 WGS84(EPSG:4326, lon/lat 순서)로 통일해 보관한다. 원천 SHP 는 EPSG:5186
(Korea 2000 중부원점 2010)이므로 build 시 변환해서 넣는다.
"""
from __future__ import annotations

import json
import os

STAIRS = "stairs"
WALL = "wall"
FENCE = "fence"
FURNITURE = "street_furniture"

# 신설 링크가 이 분류와 교차하면 무조건 배제한다 (설계서 H3·H4).
BLOCKING = (STAIRS, WALL, FENCE)
# 관통 길이가 이 값 이상이면 "링크가 계단을 지난다"고 본다.
STAIRS_CROSS_MIN_M = 3.0
# 재분류 판정 가드 (실측 2026-09-04)
#  · 그래프 링크는 geometry 가 없어 두 노드를 잇는 직선으로 취급된다. 원천 OSM 은
#    곡선이므로 length_m 이 직선거리보다 길다. 그 비가 크면 직선 근사를 믿을 수 없다.
#  · 관통 길이가 링크의 일부에 그치면 계단 옆을 스친 것일 수 있다.
# 두 가드를 걸면 안양 51개 후보 중 18개만 남는다(sidewalk 17·road 1, 전부 OSM 유래).
STRAIGHTNESS_MAX = 1.15      # length_m / 직선거리
OVERLAP_RATIO_MIN = 0.30     # 관통길이 / 직선거리
# 점형 지물은 이 반경 안을 지나면 유효폭을 잠식한다고 본다.
FURNITURE_CLEARANCE_M = 0.6

_KY = 110_540.0            # 위도 1도의 거리(m)


def _kx(lat: float) -> float:
    """경도 1도의 거리(m) — 위도에 따라 달라진다(안양 37.4도에서 약 88,400m)."""
    import math
    return math.cos(math.radians(lat)) * 111_320.0


def _length_m(geom, lat0: float) -> float:
    """WGS84 지오메트리의 길이(m). 소구역이라 평면 근사 — geo.point_segment_dist_m 과 같은 방식."""
    import math
    kx = _kx(lat0)

    def _line_len(coords):
        t = 0.0
        for (x1, y1), (x2, y2) in zip(coords[:-1], coords[1:]):
            t += math.hypot((x2 - x1) * kx, (y2 - y1) * _KY)
        return t

    if geom.is_empty:
        return 0.0
    if geom.geom_type == "LineString":
        return _line_len(list(geom.coords))
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        return sum(_length_m(g, lat0) for g in geom.geoms)
    return 0.0


class ObstacleIndex:
    """분류별 STRtree 색인. shapely 2.x 의 STRtree.query 는 인덱스 배열을 돌려준다."""

    def __init__(self, features: list | None = None):
        self._by_class: dict[str, list] = {}
        self._trees: dict[str, object] = {}
        for f in features or []:
            self._by_class.setdefault(f["obstacle"], []).append(f["geom"])
        self._build()

    def _build(self):
        from shapely.strtree import STRtree
        self._trees = {k: STRtree(v) for k, v in self._by_class.items() if v}

    def counts(self) -> dict:
        return {k: len(v) for k, v in self._by_class.items()}

    def _hits(self, klass: str, geom):
        tree = self._trees.get(klass)
        if tree is None:
            return []
        geoms = self._by_class[klass]
        return [geoms[i] for i in tree.query(geom) if geoms[i].intersects(geom)]

    def stairs_overlap_m(self, line) -> float:
        """링크가 계단 면형 안을 지나는 길이(m).

        계단 폴리곤끼리 겹치는 구역이 있어 교차를 그냥 더하면 중복 계상된다
        (실측에서 105m 링크에 관통 129m 가 나왔다). 합집합을 취한 뒤 길이를 잰다.
        """
        from shapely.ops import unary_union
        hits = self._hits(STAIRS, line)
        if not hits:
            return 0.0
        # 폴리곤을 먼저 합집합한 뒤 자른다. 교차 결과를 더하면 겹치는 계단 면형
        # (긴 계단이 단수별로 쪼개져 서로 겹치게 그려진 경우)이 중복 계상돼
        # 105m 링크에 관통 134m 같은 값이 나온다 (실측 2026-09-04).
        inter = line.intersection(unary_union(hits))
        return _length_m(inter, line.centroid.y)

    def crosses_stairs(self, line, min_m: float = STAIRS_CROSS_MIN_M) -> bool:
        return self.stairs_overlap_m(line) >= min_m

    def crosses_barrier(self, line) -> bool:
        """옹벽·담장·펜스 선형과 교차하는가 — 신설 링크의 하드 배제 조건."""
        return bool(self._hits(WALL, line) or self._hits(FENCE, line))

    def furniture_within(self, line, clearance_m: float = FURNITURE_CLEARANCE_M) -> bool:
        """점형 지물이 선분 주변 clearance 안에 있는가 (유효폭 잠식)."""
        buf = line.buffer(clearance_m / _kx(line.centroid.y))
        return bool(self._hits(FURNITURE, buf))

    def blocks_new_link(self, line) -> str | None:
        """신설 링크 하드 게이트. 배제 사유 문자열, 통과면 None."""
        if self.crosses_stairs(line, min_m=0.5):
            return "계단 관통"
        if self.crosses_barrier(line):
            return "옹벽·담장 관통"
        if self.furniture_within(line):
            return "가로시설물 저촉"
        return None

    # ---- 직렬화 -------------------------------------------------------
    def to_geojson(self, path: str) -> int:
        from shapely.geometry import mapping
        feats = []
        for klass, geoms in self._by_class.items():
            for g in geoms:
                feats.append({"type": "Feature", "properties": {"obstacle": klass},
                              "geometry": mapping(g)})
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"type": "FeatureCollection", "features": feats}, fp)
        return len(feats)

    @classmethod
    def from_geojson(cls, path: str) -> "ObstacleIndex":
        from shapely.geometry import shape
        with open(path, encoding="utf-8") as fp:
            fc = json.load(fp)
        feats = [{"obstacle": f["properties"]["obstacle"], "geom": shape(f["geometry"])}
                 for f in fc.get("features", [])]
        return cls(feats)


def build_index(sheet_paths, to_wgs84=None) -> ObstacleIndex:
    """도엽 목록에서 장애물 색인을 만든다. to_wgs84 는 (x, y) -> (lon, lat) 변환 함수."""
    from shapely.ops import transform

    from .extract import extract_obstacles

    feats = []
    for p in sheet_paths:
        for f in extract_obstacles(p):
            g = f["geom"]
            if to_wgs84 is not None:
                g = transform(lambda x, y, z=None: to_wgs84(x, y), g)
            feats.append({"obstacle": f["obstacle"], "geom": g})
    return ObstacleIndex(feats)
