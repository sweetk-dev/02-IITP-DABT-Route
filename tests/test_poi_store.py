# -*- coding: utf-8 -*-
"""PoiStore 버스 정류장 조회 — 이슈 #31.

db 백엔드는 전국 범위 정류장을 담으므로, 반경 조회가 bbox 로 선필터되는지와
행 정규화가 계약대로인지 DB 없이 검증한다.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from route_service.poi.store import PoiStore  # noqa: E402


class _RecordingStore(PoiStore):
    """_query 를 가로채 SQL·파라미터를 기록하고 고정 행을 돌려준다."""

    def __init__(self, rows):
        super().__init__(backend="db", dsn="postgresql+psycopg2://x/y")
        self._rows = rows
        self.last_sql = None
        self.last_params = None

    def _query(self, sql, params):
        self.last_sql = sql
        self.last_params = params
        return self._rows


ROW = {
    "poi_id": 208000363,
    "name": "안양박물관.김중업건축박물관",
    "latitude": 37.4175833,
    "longitude": 126.91805,
    "mobile_no": " 09327",
    "center_yn": "N",
    "route_names": "2,11,2",
}


def test_stops_normalizes_db_row():
    store = _RecordingStore([ROW])
    out = store._stops(lat=37.4176, lng=126.9180, radius_m=500)
    assert len(out) == 1
    s = out[0]
    assert s["poi_id"] == "208000363"
    assert s["lat"] == pytest.approx(37.4175833)
    assert s["lng"] == pytest.approx(126.91805)
    # 응답의 정류소번호에는 선행 공백이 붙어 온다 — 저장·반환 전에 제거되어야 한다
    assert s["mobile_no"] == "09327"
    assert s["center_yn"] is False
    # 경유 노선은 중복 제거 후 정렬
    assert s["routes"] == ["11", "2"]


def test_stops_applies_bbox_prefilter():
    store = _RecordingStore([])
    store._stops(lat=37.4, lng=126.92, radius_m=1000)
    assert "s.latitude BETWEEN" in store.last_sql
    assert "s.longitude BETWEEN" in store.last_sql
    p = store.last_params
    assert p["min_lat"] < 37.4 < p["max_lat"]
    assert p["min_lng"] < 126.92 < p["max_lng"]
    # 위도 1도가 경도 1도보다 길므로 경도 폭이 더 넓어야 한다
    assert (p["max_lng"] - p["min_lng"]) > (p["max_lat"] - p["min_lat"])


def test_stops_single_lookup_uses_poi_id_filter():
    """목적지 해석은 전체 목록을 받아 순회하지 않고 단건으로 조회한다."""
    store = _RecordingStore([ROW])
    store._stops(poi_id="208000363")
    assert "s.station_id::text = :poi_id" in store.last_sql
    assert store.last_params["poi_id"] == "208000363"
    assert "BETWEEN" not in store.last_sql


def test_stops_center_lane_flag_is_parsed():
    store = _RecordingStore([dict(ROW, center_yn="Y")])
    assert store._stops(poi_id="1")[0]["center_yn"] is True


def test_stops_none_backend_returns_empty():
    assert PoiStore(backend="none")._stops(lat=37.4, lng=126.9, radius_m=500) == []
