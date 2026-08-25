# -*- coding: utf-8 -*-
"""멀티모달(제약형) 플래너 테스트 (#36).

  1) 조합 탐색(순수) — 직결 방향 판정·회차 순번·지하철 홉
  2) API 계약 — mode=walk 호환 유지 / walk_bus legs·경고·추정 표기 / 오류 응답
"""
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import make_graph  # noqa: E402

from route_service.transit import planner as tp  # noqa: E402


def _stop(pid, name, lat, lng, routes, **kw):
    d = {"poi_id": pid, "name": name, "lat": lat, "lng": lng,
         "mobile_no": kw.get("mobile_no"), "center_yn": kw.get("center_yn", False),
         "routes": routes}
    return d


R100 = {"route_id": "R100", "name": "2", "type": "마을버스", "end_station": "테스트종점"}


def _mk_stops():
    return [
        _stop("BS-O", "출발정류장", 37.3900, 126.9503,
              [dict(R100, station_seq=[5])]),
        _stop("BS-D", "도착정류장", 37.3900, 126.9512,
              [dict(R100, station_seq=[9])], center_yn=True),
        # 반대 방향(순번이 더 작은) 정류장 — 선택되면 안 된다
        _stop("BS-R", "역방향정류장", 37.3901, 126.9512,
              [dict(R100, station_seq=[3])]),
    ]


STATIONS = [
    {"poi_id": "ST-A", "name": "안양", "lat": 37.3901, "lng": 126.9502,
     "elevator_cnt": 4, "wheelchair_lift_cnt": 0},
    {"poi_id": "ST-M", "name": "명학", "lat": 37.3908, "lng": 126.9510,
     "elevator_cnt": 0, "wheelchair_lift_cnt": 0},
]


# ── 1. 순수 탐색 ─────────────────────────────────────────────
def test_direct_bus_direction():
    stops = _mk_stops()
    cands = tp.search((37.3900, 126.9500), (37.3909, 126.9511), "walk_bus",
                      stops_near=lambda la, ln, r: stops, stations=[])
    assert cands, "직결 후보 없음"
    bus = [p for p in cands[0]["parts"] if p["kind"] == "bus"][0]
    assert bus["board"]["poi_id"] == "BS-O" and bus["seq_from"] == 5
    assert bus["alight"]["poi_id"] in ("BS-D", "BS-R")
    # 방향: 승차 순번 < 하차 순번이어야 한다
    assert bus["seq_from"] < bus["seq_to"]
    assert bus["alight"]["poi_id"] == "BS-D", "역방향 정류장이 선택됨"


def test_loop_route_multi_seq_picks_min_hops():
    """회차 노선 — 순번 [2, 40] 인 정류장에서 목적지 순번 10 이면 2→10 을 골라야 한다."""
    stops = [
        _stop("BS-L", "회차정류장", 37.3900, 126.9503, [dict(R100, station_seq=[2, 40])]),
        _stop("BS-T", "목적정류장", 37.3900, 126.9512, [dict(R100, station_seq=[10])]),
    ]
    cands = tp.search((37.3900, 126.9500), (37.3909, 126.9511), "walk_bus",
                      stops_near=lambda la, ln, r: stops, stations=[])
    bus = [p for p in cands[0]["parts"] if p["kind"] == "bus"][0]
    assert (bus["seq_from"], bus["seq_to"], bus["stop_cnt"]) == (2, 10, 8)


def test_subway_hop_same_line_only():
    cands = tp.search((37.3901, 126.9501), (37.3909, 126.9511), "walk_bus_subway",
                      stops_near=lambda la, ln, r: [], stations=STATIONS)
    assert cands, "지하철 후보 없음"
    sub = [p for p in cands[0]["parts"] if p["kind"] == "subway"][0]
    assert sub["line"] == "1호선" and sub["station_cnt"] == 1
    assert sub["board"]["name"] == "안양" and sub["alight"]["name"] == "명학"


def test_walk_bus_mode_excludes_subway():
    cands = tp.search((37.3901, 126.9501), (37.3909, 126.9511), "walk_bus",
                      stops_near=lambda la, ln, r: [], stations=STATIONS)
    assert cands == [], "walk_bus 모드에 지하철 후보가 섞임"


# ── 2. API 계약 ─────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import pickle

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)

    poi_dir = tmp_path / "poi"
    poi_dir.mkdir()
    (poi_dir / "tour_bf.json").write_text(json.dumps([{
        "poi_id": "TBF-1", "name": "테스트 무장애 공원",
        "addr": "경기도 안양시 만안구 테스트로 1",
        "latitude": 37.3909, "longitude": 126.9511,
        "dis_toilet_yn": "Y", "elevator_yn": "Y", "slope_yn": "Y",
        "dis_parking_yn": "Y",
        "entrance": {"lat": 37.3909, "lng": 126.9511},
    }], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "stations.json").write_text(json.dumps([
        {"poi_id": "ST-A", "name": "안양", "latitude": 37.3901, "longitude": 126.9502,
         "elevator_cnt": 4, "wheelchair_lift_cnt": 0},
        {"poi_id": "ST-M", "name": "명학", "latitude": 37.3908, "longitude": 126.9510,
         "elevator_cnt": 0, "wheelchair_lift_cnt": 0},
    ], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "transit_stops.json").write_text(json.dumps([
        # 승차 정류장은 출발지 지척(N1 스냅) — 첫 도보 leg 는 생략돼야 한다
        {"poi_id": "BS-O", "name": "출발정류장", "lat": 37.3900, "lng": 126.9502,
         "mobile_no": "09001",
         "routes": [{"route_id": "R100", "name": "2", "type": "마을버스",
                     "end_station": "테스트종점", "station_seq": [5]}]},
        # 하차 정류장은 N2 권역 — 목적지(N3)까지 실제 도보 leg 가 생겨야 한다
        {"poi_id": "BS-D", "name": "도착정류장", "lat": 37.3900, "lng": 126.9512,
         "mobile_no": "09002", "center_yn": "Y",
         "routes": [{"route_id": "R100", "name": "2", "type": "마을버스",
                     "end_station": "테스트종점", "station_seq": [9]}]},
    ], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "transit_route_paths.json").write_text(json.dumps([
        {"route_id": "R100", "station_seq": 5, "name": "출발정류장",
         "mobile_no": "09001", "lat": 37.3900, "lng": 126.9502},
        {"route_id": "R100", "station_seq": 6, "name": "경유1",
         "mobile_no": "09011", "lat": 37.3899, "lng": 126.9505},
        {"route_id": "R100", "station_seq": 7, "name": "경유2",
         "mobile_no": "09012", "lat": 37.3899, "lng": 126.9508},
        {"route_id": "R100", "station_seq": 8, "name": "경유3",
         "mobile_no": "09013", "lat": 37.3899, "lng": 126.9510},
        {"route_id": "R100", "station_seq": 9, "name": "도착정류장",
         "mobile_no": "09002", "lat": 37.3900, "lng": 126.9512},
    ], ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")

    import importlib

    import route_service.config as cfg
    # Settings 는 dataclass 기본값을 클래스 정의 시점의 env 로 고정한다 —
    # 모듈 자체를 reload 해야 monkeypatch 된 env 가 반영된다.
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c


def _plan(client, mode, origin=(37.3900, 126.9500)):
    return client.post("/route/plan", json={
        "origin": {"lat": origin[0], "lng": origin[1]},
        "destination": {"type": "tour", "poi_id": "TBF-1"},
        "profile": "wheelchair_manual",
        "mode": mode,
    })


def test_walk_mode_response_unchanged(client):
    body = _plan(client, "walk").json()
    assert "legs" not in body["routes"][0], "walk 응답에 legs 가 생김(호환 파괴)"
    assert "mode" not in body


def test_walk_bus_legs_contract(client):
    r = _plan(client, "walk_bus")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "walk_bus"
    legs = body["routes"][0]["legs"]
    kinds = [l["kind"] for l in legs]
    assert kinds == ["bus", "walk"], kinds     # 첫 도보는 지척이라 생략
    bus = legs[0]
    assert bus["route"]["route_id"] == "R100" and bus["route"]["type"] == "마을버스"
    assert bus["board"]["station_seq"] == 5 and bus["alight"]["station_seq"] == 9
    assert bus["stop_cnt"] == 4 and len(bus["stops"]) == 5
    assert any("저상버스" in w for w in bus["warnings"])
    assert any("중앙차로" in w for w in bus["warnings"])   # 하차 정류장 center_yn

    s = body["routes"][0]["summary"]
    assert s["transit"]["bus_cnt"] == 1 and s["transit"]["stop_cnt"] == 4
    assert "추정" in s["eta_note"]
    assert s["walk_distance_m"] > 0

    steps = body["routes"][0]["steps"]
    mans = [st["maneuver"] for st in steps]
    assert "bus_board" in mans and "bus_alight" in mans
    assert mans[-1] == "arrive"
    board = steps[mans.index("bus_board")]
    assert "테스트종점 방면" in board["instruction"]
    assert [st["idx"] for st in steps] == list(range(len(steps)))

    # geometry 는 leg 연결본 — 버스 경유 정류장 좌표를 포함해야 한다
    geom = body["routes"][0]["geometry"]
    assert [37.3899, 126.9508] in [[round(a, 4), round(b, 4)] for a, b in geom]


def test_walk_bus_subway_prefers_direct_when_better(client):
    r = _plan(client, "walk_bus_subway")
    assert r.status_code == 200, r.text
    kinds = [l["kind"] for l in r.json()["routes"][0]["legs"]]
    assert "bus" in kinds or "subway" in kinds


def test_no_transit_candidates_404(client):
    r = _plan(client, "walk_bus", origin=(37.5000, 127.1000))
    assert r.status_code in (404, 422)
    assert "도보" in r.json()["detail"] or "경로" in r.json()["detail"]


def test_unknown_mode_400(client):
    r = _plan(client, "taxi")
    assert r.status_code == 400
