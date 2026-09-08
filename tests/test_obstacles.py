# -*- coding: utf-8 -*-
"""통행 장애물 색인 (v1.21.0)."""
from __future__ import annotations

import pytest

shapely = pytest.importorskip("shapely")
from shapely.geometry import LineString, Point, Polygon  # noqa: E402

from route_service.topomap.obstacles import (FENCE, FURNITURE,  # noqa: E402
                                             STAIRS, WALL, ObstacleIndex)

LAT, LON = 37.3900, 126.9500
# 안양 위도에서 경도 1도 = 약 88,400m, 위도 1도 = 110,540m
DLON_10M = 10.0 / 88_400.0
DLAT_10M = 10.0 / 110_540.0


def _box(lon0, lon1):
    """위도는 고정하고 경도 구간만 잡는 가로 띠 폴리곤."""
    return Polygon([(lon0, LAT - DLAT_10M), (lon1, LAT - DLAT_10M),
                    (lon1, LAT + DLAT_10M), (lon0, LAT + DLAT_10M)])


def _east_line(meters):
    return LineString([(LON, LAT), (LON + meters / 88_400.0, LAT)])


def test_stairs_overlap_measures_metres():
    idx = ObstacleIndex([{"obstacle": STAIRS, "geom": _box(LON, LON + DLON_10M)}])
    assert idx.stairs_overlap_m(_east_line(50)) == pytest.approx(10.0, abs=0.5)


def test_overlapping_stair_polygons_are_not_double_counted():
    """긴 계단이 단수별로 겹치게 그려진 경우 — 교차를 더하면 링크보다 길어진다.

    실측(2026-09-04)에서 105m 링크에 관통 134m 가 나왔던 회귀.
    """
    a = _box(LON, LON + 3 * DLON_10M)              # 0~30m
    b = _box(LON + DLON_10M, LON + 4 * DLON_10M)   # 10~40m (a 와 20m 겹침)
    idx = ObstacleIndex([{"obstacle": STAIRS, "geom": a}, {"obstacle": STAIRS, "geom": b}])
    ov = idx.stairs_overlap_m(_east_line(100))
    assert ov == pytest.approx(40.0, abs=1.0)      # 30+30 이 아니라 합집합 40
    assert ov <= 100.0


def test_crosses_stairs_threshold():
    idx = ObstacleIndex([{"obstacle": STAIRS, "geom": _box(LON, LON + DLON_10M)}])
    assert idx.crosses_stairs(_east_line(50), min_m=3.0)
    assert not idx.crosses_stairs(_east_line(50), min_m=30.0)


def test_barrier_and_furniture_gates():
    wall = LineString([(LON + 2 * DLON_10M, LAT - DLAT_10M),
                       (LON + 2 * DLON_10M, LAT + DLAT_10M)])
    tree = Point(LON + 5 * DLON_10M, LAT + 0.3 / 110_540.0)   # 선에서 30cm
    idx = ObstacleIndex([{"obstacle": WALL, "geom": wall},
                         {"obstacle": FURNITURE, "geom": tree}])
    assert idx.crosses_barrier(_east_line(100))
    assert idx.furniture_within(_east_line(100), clearance_m=0.6)
    assert not idx.furniture_within(_east_line(100), clearance_m=0.1)


def test_blocks_new_link_reports_reason():
    idx = ObstacleIndex([{"obstacle": STAIRS, "geom": _box(LON, LON + DLON_10M)}])
    assert idx.blocks_new_link(_east_line(50)) == "계단 관통"
    assert idx.blocks_new_link(LineString([(LON - 0.01, LAT), (LON - 0.009, LAT)])) is None


def test_empty_index_blocks_nothing():
    idx = ObstacleIndex([])
    assert idx.counts() == {}
    assert idx.blocks_new_link(_east_line(50)) is None
    assert idx.stairs_overlap_m(_east_line(50)) == 0.0


def test_geojson_roundtrip(tmp_path):
    idx = ObstacleIndex([{"obstacle": STAIRS, "geom": _box(LON, LON + DLON_10M)},
                         {"obstacle": FENCE, "geom": _east_line(20)}])
    p = tmp_path / "obst.geojson"
    assert idx.to_geojson(str(p)) == 2
    back = ObstacleIndex.from_geojson(str(p))
    assert back.counts() == idx.counts()
    assert back.stairs_overlap_m(_east_line(50)) == pytest.approx(10.0, abs=0.5)
