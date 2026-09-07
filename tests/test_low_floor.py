# -*- coding: utf-8 -*-
"""저상버스 우선 모드 (#64, v1.22.0).

  1) 판정기 — 도착정보 저상 확인(tier 1) / 놓치는 차량 제외 / 위치정보 상류 추정(tier 2, 순환 보정)
             / 저상 없음(tier 3) / 실시간 장애(tier 0) / 운행 종료 flag 제외
  2) 1차 필터 — 전일 기준 low_bus_yn='N' 은 후보 제외, 오래된 표는 무시, NULL 통과
  3) API 계약 — 휠체어 프로필 기본 on: 가까운 일반버스 정류장 대신 저상이 오는 정류장·노선을 고른다.
     low_floor=false / 시각장애 프로필은 종전과 같다. 저상이 없으면 404 가 아니라 일반 경로 + 경고.
"""
import datetime
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import make_graph  # noqa: E402

from route_service.transit import gbis_live, low_floor, planner as tp  # noqa: E402


# ── 1. 판정기 ─────────────────────────────────────────────
class _Live:
    enabled = True

    def __init__(self, arrivals=None, locations=None):
        self._a = arrivals or {}
        self._l = locations or {}
        self.arr_calls, self.loc_calls = [], []

    def arrivals(self, station_id, route_id=None, route_meta=None):
        self.arr_calls.append(str(station_id))
        return self._a.get(str(station_id), {"status": "unavailable", "reason": "down", "items": []})

    def locations(self, route_id, stop_index=None):
        self.loc_calls.append(str(route_id))
        return self._l.get(str(route_id), {"status": "unavailable", "reason": "down", "vehicles": []})


class _Store:
    def __init__(self, route_len=None):
        self._len = route_len or {}

    def stop_route_meta(self, sid):
        return {}

    def route_stops(self, rid):
        n = self._len.get(str(rid), 0)
        return [{"station_seq": i} for i in range(1, n + 1)]


def _part(route_id="R6", seq_from=5, station="S1"):
    return {"kind": "bus", "route": {"route_id": route_id, "name": "6"},
            "board": {"poi_id": station, "lat": 37.39, "lng": 126.95}, "seq_from": seq_from}


def _arr(route_id, vehicles, flag="PASS"):
    return {"status": "success", "items": [{"route_id": route_id, "flag": flag, "vehicles": vehicles}]}


def test_judge_tier1_picks_first_low_floor_that_can_be_caught():
    live = _Live(arrivals={"S1": _arr("R6", [
        {"predict_min": 2, "predict_sec": 120, "low_floor": True, "stops_away": 1, "plate_no": "A"},   # 걸어가면 놓친다
        {"predict_min": 9, "predict_sec": 540, "low_floor": True, "stops_away": 6, "plate_no": "B"},
    ])})
    j = low_floor.LowFloorJudge(live, _Store()).judge(_part(), walk_to_board_sec=240)
    assert j["tier"] == 1 and j["plate_no"] == "B"
    assert j["wait_sec"] == pytest.approx(300)      # 540 - 240


def test_judge_uses_locations_when_both_arrivals_are_regular_and_handles_loop():
    live = _Live(
        arrivals={"S1": _arr("R6", [{"predict_min": 3, "low_floor": False}, {"predict_min": 8, "low_floor": False}])},
        locations={"R6": {"status": "success", "vehicles": [
            {"station_seq": 3, "low_floor": False},
            {"station_seq": 38, "low_floor": True, "plate_no": "LOOP"},    # 순환: 40 정거장 노선, 승차 순번 5 → 7 정거장 뒤
        ]}})
    j = low_floor.LowFloorJudge(live, _Store({"R6": 40})).judge(_part(seq_from=5), walk_to_board_sec=60)
    assert j["tier"] == 2 and j["stops_away"] == 7 and j["plate_no"] == "LOOP"
    assert live.loc_calls == ["R6"]


def test_judge_tier3_when_no_low_floor_in_service():
    live = _Live(arrivals={"S1": _arr("R10", [{"predict_min": 4, "low_floor": False}])},
                 locations={"R10": {"status": "success", "vehicles": [{"station_seq": 2, "low_floor": False}]}})
    j = low_floor.LowFloorJudge(live, _Store()).judge(_part("R10"), 60)
    assert j["tier"] == 3 and j["wait_sec"] is None


def test_judge_tier0_when_realtime_unavailable_and_flag_excluded():
    j = low_floor.LowFloorJudge(_Live(), _Store()).judge(_part(), 60)
    assert j["tier"] == 0
    live = _Live(arrivals={"S1": _arr("R6", [{"predict_min": 9, "low_floor": True}], flag="STOP")})
    j = low_floor.LowFloorJudge(live, _Store()).judge(_part(), 60)
    assert j["tier"] != 1, "운행 종료 flag 의 차량을 저상 확인으로 세면 안 된다"


def test_rank_order_tier_then_time():
    assert low_floor.rank_key(1, 900) < low_floor.rank_key(2, 100)
    assert low_floor.rank_key(2, 900) < low_floor.rank_key(0, 100)
    assert low_floor.rank_key(0, 900) < low_floor.rank_key(3, 100)


# ── 2. 1차 필터 ───────────────────────────────────────────
def test_low_bus_route_ok_filter():
    today = datetime.date(2026, 9, 7)
    assert tp.low_bus_route_ok({"low_bus_yn": "Y"}, today)
    assert tp.low_bus_route_ok({"low_bus_yn": None}, today)
    assert not tp.low_bus_route_ok({"low_bus_yn": "N", "low_bus_base_dt": "2026-09-06"}, today)
    assert tp.low_bus_route_ok({"low_bus_yn": "N", "low_bus_base_dt": "2026-08-01"}, today), "오래된 표는 무시"
    assert not tp.low_bus_route_ok({"low_bus_yn": "N"}, today)


def test_search_radius_and_route_filter():
    far = {"poi_id": "FAR", "name": "먼정류장", "lat": 37.3955, "lng": 126.9500,   # 약 610m
           "routes": [{"route_id": "R6", "name": "6", "station_seq": [3]}]}
    dest = {"poi_id": "D", "name": "도착", "lat": 37.3909, "lng": 126.9520,
            "routes": [{"route_id": "R6", "name": "6", "station_seq": [8]},
                       {"route_id": "R10", "name": "10-1", "station_seq": [9], "low_bus_yn": "N",
                        "low_bus_base_dt": datetime.date.today().isoformat()}]}
    near = {"poi_id": "NEAR", "name": "가까운정류장", "lat": 37.3901, "lng": 126.9500,
            "routes": [{"route_id": "R10", "name": "10-1", "station_seq": [4], "low_bus_yn": "N",
                        "low_bus_base_dt": datetime.date.today().isoformat()}]}
    stops = [far, dest, near]
    sn = lambda la, ln, r: stops
    o, t = (37.3900, 126.9500), (37.3909, 126.9521)
    base = tp.search(o, t, "walk_bus", sn, [])
    assert [c["parts"][1]["route"]["route_id"] for c in base] == ["R10"], "450m 기본 반경에는 가까운 10-1 만"
    filtered = tp.search(o, t, "walk_bus", sn, [], route_ok=tp.low_bus_route_ok)
    assert filtered == [], "low_bus_yn=N 노선은 후보에서 빠진다"
    wide = tp.search(o, t, "walk_bus", sn, [], stop_radius_m=800, max_stops=10, route_ok=tp.low_bus_route_ok)
    assert [c["parts"][1]["route"]["route_id"] for c in wide] == ["R6"], "800m 로 넓히면 저상 6번 정류장이 들어온다"


# ── 3. API 계약 ───────────────────────────────────────────
def _arrival_body(station_id, route_id, name, lows, mins):
    return {"response": {"msgHeader": {"resultCode": 0}, "msgBody": {"busArrivalList": [
        {"flag": "PASS", "routeId": route_id, "routeName": name, "stationId": station_id, "staOrder": 5,
         "locationNo1": 3, "locationNo2": 8, "lowPlate1": lows[0], "lowPlate2": lows[1],
         "plateNo1": "P1", "plateNo2": "P2", "predictTime1": mins[0], "predictTime2": mins[1],
         "predictTimeSec1": mins[0] * 60, "predictTimeSec2": mins[1] * 60, "routeDestName": "종점"}]}}}


def _location_body(route_id, lows):
    return {"response": {"msgHeader": {"resultCode": 0}, "msgBody": {"busLocationList": [
        {"routeId": route_id, "lowPlate": lp, "plateNo": "L%d" % i, "stationId": "X", "stationSeq": i + 1, "stateCd": 0}
        for i, lp in enumerate(lows)]}}}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import pickle

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)
    poi_dir = tmp_path / "poi"
    poi_dir.mkdir()
    (poi_dir / "tour_bf.json").write_text(json.dumps([{
        "poi_id": "TBF-1", "name": "테스트 시청", "addr": "경기도 안양시 만안구 테스트로 1",
        "latitude": 37.3909, "longitude": 126.9511, "entrance": {"lat": 37.3909, "lng": 126.9511},
    }], ensure_ascii=False), encoding="utf-8")
    (poi_dir / "stations.json").write_text("[]", encoding="utf-8")
    # 가까운 정류장(N1 지척)에는 10-1 만, 55m 떨어진 정류장(N4 근처)에는 저상 6번이 선다.
    (poi_dir / "transit_stops.json").write_text(json.dumps([
        {"poi_id": "BS-NEAR", "name": "명학성당", "lat": 37.3900, "lng": 126.9502, "mobile_no": "09286",
         "routes": [{"route_id": "R10", "name": "10-1", "type": "마을버스", "end_station": "성원상떼빌", "station_seq": [5]}]},
        {"poi_id": "BS-FAR", "name": "만안평생교육센터", "lat": 37.3904, "lng": 126.9496, "mobile_no": "09167",
         "routes": [{"route_id": "R6", "name": "6", "type": "일반형시내버스", "end_station": "벌말초교", "station_seq": [5]}]},
        {"poi_id": "BS-D", "name": "안양시청", "lat": 37.3900, "lng": 126.9512, "mobile_no": "10095",
         "routes": [{"route_id": "R10", "name": "10-1", "type": "마을버스", "end_station": "성원상떼빌", "station_seq": [9]},
                    {"route_id": "R6", "name": "6", "type": "일반형시내버스", "end_station": "벌말초교", "station_seq": [11]}]},
    ], ensure_ascii=False), encoding="utf-8")
    paths = []
    for rid, seqs, board in (("R10", range(5, 10), "BS-NEAR"), ("R6", range(5, 12), "BS-FAR")):
        for i, sq in enumerate(seqs):
            paths.append({"route_id": rid, "station_id": board if i == 0 else ("BS-D" if sq == seqs[-1] else "X%d" % sq),
                          "station_seq": sq, "name": "경유%d" % sq, "mobile_no": "0",
                          "lat": 37.3900 + 0.0001 * i, "lng": 126.9502 + 0.0002 * i})
    (poi_dir / "transit_route_paths.json").write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-1")
    monkeypatch.setenv("POI_BACKEND", "file")
    monkeypatch.setenv("POI_DATA_DIR", str(poi_dir))
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    monkeypatch.setenv("DATA_GO_KR_API_KEY", "test-key")

    import importlib
    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)

    responses = {
        "stationId=BS-NEAR": _arrival_body("BS-NEAR", "R10", "10-1", (0, 0), (4, 9)),
        "stationId=BS-FAR": _arrival_body("BS-FAR", "R6", "6", (1, 1), (7, 12)),
        "routeId=R10": _location_body("R10", [0, 0, 0]),
        "routeId=R6": _location_body("R6", [1, 1, 1]),
        "getBusRouteLineListv2": {"response": {"msgHeader": {"resultCode": 4}, "msgBody": None}},
    }
    calls = []

    def fetch(url):
        calls.append(url)
        for key, body in responses.items():
            if key in url:
                return body
        raise AssertionError("예상치 못한 URL: %s" % url)
    with TestClient(m.app) as c:
        m.gbis_live.LIVE = gbis_live.GbisLive(api_key="k", fetch=fetch, cache_ttl_sec=60)
        c.calls = calls
        c.responses = responses
        c.main = m
        yield c


def _plan(client, **extra):
    body = {"origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "tour", "poi_id": "TBF-1"},
            "profile": "wheelchair_manual", "mode": "walk_bus"}
    body.update(extra)
    return client.post("/route/plan", json=body)


def _bus(r):
    return [l for l in r.json()["routes"][0]["legs"] if l["kind"] == "bus"][0]


def test_wheelchair_default_on_picks_low_floor_route_over_nearer_regular_bus(client):
    r = _plan(client)
    assert r.status_code == 200, r.text
    body = r.json()
    bus = _bus(r)
    assert body["low_floor"]["mode"] is True and body["low_floor"]["tier"] == 1
    assert bus["route"]["route_id"] == "R6" and bus["board"]["poi_id"] == "BS-FAR"
    # 실경로(계단 회피 우회 260m)로는 7분 뒤 차량을 놓치므로 12분 뒤 두 번째 저상 차량이 잡힌다
    assert bus["low_floor"]["tier"] == 1 and bus["low_floor"]["plate_no"] == "P2"
    assert any("저상버스 6번이 약 12분" in w for w in bus["warnings"])
    assert body["routes"][0]["summary"]["duration_sec"] > 12 * 60 - 60
    assert all("보장되지 않습니다" not in w for w in bus["warnings"])
    assert body["low_floor"]["queried_at"] and body["low_floor"]["valid_for_sec"] == 120
    assert body["routes"][0]["summary"]["eta_note"].startswith("소요시간은 정거장 수 기반 추정에 저상버스 대기")
    # 도착정보는 승차 후보 정류장 단위로 — 노선 단위 반복 호출이 아니다
    arr = [c for c in client.calls if "busarrivalservice" in c]
    assert len(arr) == 2


def test_low_floor_false_keeps_static_choice(client):
    r = _plan(client, low_floor=False)
    assert r.json()["low_floor"] == {"mode": False}
    assert _bus(r)["route"]["route_id"] == "R10"
    assert [c for c in client.calls if "busarrivalservice" in c] == []


def test_visual_profile_defaults_off(client):
    r = _plan(client, profile="visual")
    assert r.json()["low_floor"] == {"mode": False}
    assert _bus(r)["route"]["route_id"] == "R10"


def test_no_low_floor_anywhere_returns_regular_route_with_warning(client):
    client.responses["stationId=BS-FAR"] = _arrival_body("BS-FAR", "R6", "6", (0, 0), (7, 12))
    client.responses["routeId=R6"] = _location_body("R6", [0, 0])
    r = _plan(client)
    assert r.status_code == 200
    body = r.json()
    assert body["low_floor"]["tier"] == 3
    assert _bus(r)["route"]["route_id"] == "R10", "저상이 없으면 정적 1순위(가까운 정류장)로 돌아간다"
    assert body["routes"][0]["summary"]["warnings"][0].startswith("현재 운행 중인 저상버스가 없습니다")


def test_realtime_down_is_tier0_not_error(client):
    client.main.gbis_live.LIVE = gbis_live.GbisLive(api_key="k", fetch=lambda u: (_ for _ in ()).throw(OSError("down")),
                                                     cache_ttl_sec=60)
    r = _plan(client)
    assert r.status_code == 200
    assert r.json()["low_floor"]["tier"] == 0
    assert r.json()["routes"][0]["summary"]["warnings"][0].startswith("실시간 저상버스 정보를 확인하지 못했습니다")


def test_static_whitelist_excludes_n_routes_before_realtime(client, tmp_path):
    poi_dir = tmp_path / "poi"
    stops = json.loads((poi_dir / "transit_stops.json").read_text(encoding="utf-8"))
    for s in stops:
        for rt in s["routes"]:
            if rt["route_id"] == "R10":
                rt["low_bus_yn"] = "N"
                rt["low_bus_base_dt"] = datetime.date.today().isoformat()
    (poi_dir / "transit_stops.json").write_text(json.dumps(stops, ensure_ascii=False), encoding="utf-8")
    client.main.poi_store.STORE._cache = {} if hasattr(client.main.poi_store.STORE, "_cache") else None
    r = _plan(client)
    assert _bus(r)["route"]["route_id"] == "R6"
    assert not any("stationId=BS-NEAR" in c for c in client.calls), "표에서 저상 없음이 확정된 노선은 실시간 조회조차 하지 않는다"
