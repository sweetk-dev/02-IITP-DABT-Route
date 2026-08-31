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
    "route_list": [
        {"route_id": 241253001, "name": "2", "type": "마을버스",
         "end_station": "안양역", "station_seq": [10, 34]},
        {"route_id": 213000017, "name": "2", "type": "일반형시내버스",
         "end_station": "군포공영차고지", "station_seq": [5]},
        {"route_id": 241253002, "name": "2-1", "type": "마을버스",
         "end_station": "안양예술공원", "station_seq": [7]},
    ],
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
    # 노선번호가 같아도 다른 노선이면 유지하고, 완전 중복만 제거한다
    assert s["routes"] == [
        {"route_id": 213000017, "name": "2", "type": "일반형시내버스",
         "end_station": "군포공영차고지", "station_seq": [5]},
        {"route_id": 241253001, "name": "2", "type": "마을버스",
         "end_station": "안양역", "station_seq": [10, 34]},
        {"route_id": 241253002, "name": "2-1", "type": "마을버스",
         "end_station": "안양예술공원", "station_seq": [7]},
    ]


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


def test_routes_keep_type_for_duplicate_numbers():
    """번호가 겹치는 노선(안양 117개 중 12쌍)을 유형으로 구별할 수 있어야 한다."""
    store = _RecordingStore([ROW])
    routes = store._stops(poi_id="1")[0]["routes"]
    same_number = [r for r in routes if r["name"] == "2"]
    assert len(same_number) == 2
    assert {r["type"] for r in same_number} == {"마을버스", "일반형시내버스"}


def test_routes_accept_plain_string_list_from_file_backend():
    """file 백엔드의 문자열 목록도 같은 형태로 정규화된다."""
    from route_service.poi.store import _normalize_routes

    assert _normalize_routes(["11", "2", "2"], None) == [
        {"route_id": None, "name": "11", "type": None,
         "end_station": None, "station_seq": []},
        {"route_id": None, "name": "2", "type": None,
         "end_station": None, "station_seq": []},
    ]


def test_routes_decode_json_string_payload():
    """드라이버가 json 을 문자열로 넘겨도 파싱되어야 한다."""
    from route_service.poi.store import _normalize_routes

    out = _normalize_routes(None, '[{"route_id": 1, "name": "9", "type": "마을버스"}]')
    assert out == [{"route_id": 1, "name": "9", "type": "마을버스",
                    "end_station": None, "station_seq": []}]


def test_routes_expose_station_seq_for_direction():
    """회차 노선은 한 정류장을 두 번 지난다 — 순번으로 진행 방향을 판정할 수 있어야 한다."""
    store = _RecordingStore([ROW])
    routes = {r["route_id"]: r for r in store._stops(poi_id="1")[0]["routes"]}
    assert routes[241253001]["station_seq"] == [10, 34]
    assert routes[213000017]["station_seq"] == [5]


def test_routes_empty_when_station_has_no_route():
    """경유 노선이 없는 정류장은 json_agg 가 NULL 을 주므로 빈 목록이어야 한다."""
    store = _RecordingStore([dict(ROW, route_list=None)])
    assert store._stops(poi_id="1")[0]["routes"] == []


def test_stops_center_lane_flag_is_parsed():
    store = _RecordingStore([dict(ROW, center_yn="Y")])
    assert store._stops(poi_id="1")[0]["center_yn"] is True


def test_stops_none_backend_returns_empty():
    assert PoiStore(backend="none")._stops(lat=37.4, lng=126.9, radius_m=500) == []


def test_routes_expose_end_station_as_direction_hint():
    """순번 산술 없이도 방면을 대조할 수 있도록 종점명을 함께 준다."""
    store = _RecordingStore([ROW])
    routes = {r["route_id"]: r for r in store._stops(poi_id="1")[0]["routes"]}
    assert routes[241253001]["end_station"] == "안양역"
    assert "r.end_station_name" in store.last_sql


def test_stop_accessible_status_is_unknown_not_false():
    """None 을 falsy 로 뭉뚱그려 '접근 불가'로 표시하지 않도록 상태를 따로 준다."""
    store = _RecordingStore([ROW])
    out = store.list_transit_access(37.4176, 126.9180, radius_m=500)
    stop = [x for x in out if x["type"] == "transit_stop"][0]
    assert stop["accessible"] is None
    assert stop["accessible_status"] == "unknown"
