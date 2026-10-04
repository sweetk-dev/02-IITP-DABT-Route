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
import time

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


# ── 4. 실시간 조회 예산 — 외부 API 가 느리거나 응답하지 않을 때 ─────────────────
def test_judge_stops_querying_after_consecutive_failures():
    """연속 실패 임계에 이르면 남은 정류장은 조회하지 않고 판정 불가(tier 0)로 둔다."""
    live = _Live()                                   # 모든 조회가 unavailable
    judge = low_floor.LowFloorJudge(live, _Store())
    tiers = [judge.judge(_part(station="S%d" % i), 60)["tier"] for i in range(6)]
    assert tiers == [0] * 6
    assert len(live.arr_calls) == gbis_live.CONSECUTIVE_FAIL_LIMIT, live.arr_calls
    assert judge.realtime_unavailable is True
    assert judge.arrivals("S5").get("skipped") is True


def test_judge_success_resets_failure_streak_and_normal_results_unchanged():
    """실패 사이에 성공이 끼면 연속 실패가 아니다 — 정상 응답의 판정은 예산이 없을 때와 같다."""
    ok = _arr("R6", [{"low_floor": True, "predict_sec": 600, "predict_min": 10, "plate_no": "A"}])
    live = _Live(arrivals={"S1": ok, "S3": ok, "S5": ok})
    judge = low_floor.LowFloorJudge(live, _Store())
    tiers = [judge.judge(_part(station="S%d" % i), 60)["tier"] for i in range(6)]
    assert tiers == [0, 1, 0, 1, 0, 1]
    assert live.arr_calls == ["S%d" % i for i in range(6)]
    assert judge.realtime_unavailable is False


def test_judge_skips_remaining_queries_when_time_budget_is_used():
    ok = _arr("R6", [{"low_floor": True, "predict_sec": 600, "predict_min": 10, "plate_no": "A"}])
    t = [0.0]

    class _SlowOk(_Live):
        def arrivals(self, station_id, route_id=None, route_meta=None):
            t[0] += 0.9                              # 조회 한 번에 0.9초가 걸리는 상황(가짜 시계)
            self.arr_calls.append(str(station_id))
            return ok
    live = _SlowOk()
    judge = low_floor.LowFloorJudge(live, _Store(),
                                   budget=gbis_live.LiveBudget(budget_sec=2.0, clock=lambda: t[0]))
    tiers = [judge.judge(_part(station="S%d" % i), 60)["tier"] for i in range(6)]
    assert tiers == [1, 1, 1, 0, 0, 0], "예산(2초)을 넘긴 뒤의 정류장은 조회 없이 판정 불가"
    assert live.arr_calls == ["S0", "S1", "S2"]
    assert "budget" in judge.arrivals("S4")["reason"]


def test_judge_turns_live_exception_into_unavailable():
    class _Boom(_Live):
        def arrivals(self, station_id, route_id=None, route_meta=None):
            raise RuntimeError("boom")
    j = low_floor.LowFloorJudge(_Boom(), _Store()).judge(_part(), 60)
    assert j["tier"] == 0 and "boom" in j["reason"]


def test_budget_caps_http_timeout_to_remaining_budget(monkeypatch):
    """예산 안에서 실행하는 조회는 타임아웃이 남은 예산으로 낮아진다(예산 밖 조회는 설정값 그대로)."""
    seen = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"response": {"msgHeader": {"resultCode": 4}}}'

    def fake_urlopen(req, timeout=None):
        seen.append(timeout)
        return _Resp()
    monkeypatch.setattr(gbis_live.urllib.request, "urlopen", fake_urlopen)
    live = gbis_live.GbisLive(api_key="k", timeout_sec=3.0, cache_ttl_sec=0)
    live.arrivals("S1")
    b = gbis_live.LiveBudget(budget_sec=2.0)
    b.call(live.arrivals, "S2")
    b.spent_sec = 1.9                                # 예산이 0.1초 남았을 때는 최소 타임아웃을 준다
    b.call(live.arrivals, "S3")
    assert seen[0] == 3.0
    assert seen[1] == pytest.approx(2.0, abs=0.05)
    assert seen[2] == gbis_live.MIN_CALL_TIMEOUT_SEC
    live.arrivals("S4")
    assert seen[3] == 3.0, "예산 실행이 끝나면 상한이 풀려야 한다"


def test_gbis_incomplete_read_and_non_dict_json_are_unavailable_not_exception():
    import http.client

    def cut(url):
        raise http.client.IncompleteRead(b"{")
    for fetch in (cut, lambda u: [1, 2], lambda u: "oops", lambda u: {"response": "x"},
                  lambda u: {"response": {"msgHeader": []}},
                  lambda u: {"response": {"msgHeader": {"resultCode": 0}, "msgBody": [1]}}):
        live = gbis_live.GbisLive(api_key="k", fetch=fetch)
        a = live.arrivals("S1")
        assert a["status"] == "unavailable" and a["items"] == []
        assert live.locations("R1")["status"] == "unavailable"
        assert live.route_line("R1") == []
    # 목록 자리에 dict 가 아닌 항목이 섞여 와도 정상 항목만 쓴다
    body = {"response": {"msgHeader": {"resultCode": 0}, "msgBody": {
        "busArrivalList": ["x", {"routeId": "R1", "plateNo1": "P", "predictTime1": 3, "lowPlate1": 1}],
        "busLocationList": "x", "busRouteLineList": [None]}}}
    live = gbis_live.GbisLive(api_key="k", fetch=lambda u: body)
    assert [it["route_id"] for it in live.arrivals("S1")["items"]] == ["R1"]
    assert live.locations("R1")["vehicles"] == []
    assert live.route_line("R1") == []


class _FakeLive(gbis_live.GbisLive):
    """조회마다 delay 초가 걸리는 가짜 실시간 — mode 'fail'(실패)·'ok'(저상 확인)·'raise'(예외)."""

    def __init__(self, delay=0.0, mode="fail"):
        super().__init__(api_key="k")
        self.delay, self.mode = delay, mode
        self.arr_calls, self.loc_calls, self.line_calls = [], [], []

    def _wait(self):
        if self.delay:
            time.sleep(self.delay)

    def arrivals(self, station_id, route_id=None, route_meta=None):
        self.arr_calls.append(str(station_id))
        self._wait()
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "fail":
            return {"status": "unavailable", "reason": "TimeoutError: timed out", "station_id": str(station_id),
                    "items": [], "next_low_floor": None}
        rid = "R%s" % str(station_id).split("-")[-1]
        v = {"order": 1, "low_floor": True, "predict_sec": 900, "predict_min": 15, "stops_away": 4, "plate_no": "LF"}
        items = [{"route_id": rid, "route_name": rid, "flag": "PASS", "vehicles": [v]}]
        return {"status": "success", "station_id": str(station_id), "items": items,
                "next_low_floor": self.next_low_floor(items)}

    def locations(self, route_id, stop_index=None):
        self.loc_calls.append(str(route_id))
        self._wait()
        return {"status": "unavailable", "reason": "down", "route_id": str(route_id), "vehicles": [], "low_floor_cnt": 0}

    def route_line(self, route_id):
        self.line_calls.append(str(route_id))
        self._wait()
        return []


N_BOARD_STOPS = 8


@pytest.fixture()
def busy_client(tmp_path, monkeypatch):
    """출발지 주변에 승차 후보 정류장이 8곳(노선도 제각각)인 구성 — 후보마다 실시간 조회가 따로 나간다."""
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
    stops, paths = [], []
    dest_routes = []
    for i in range(N_BOARD_STOPS):
        rid = "R%d" % i
        stops.append({"poi_id": "BS-%d" % i, "name": "승차%d" % i, "lat": 37.3900 + 0.00002 * i, "lng": 126.9502,
                      "mobile_no": "0900%d" % i,
                      "routes": [{"route_id": rid, "name": str(i), "type": "일반형시내버스",
                                  "end_station": "종점", "station_seq": [5]}]})
        dest_routes.append({"route_id": rid, "name": str(i), "type": "일반형시내버스",
                            "end_station": "종점", "station_seq": [9]})
        for k, sq in enumerate(range(5, 10)):
            paths.append({"route_id": rid, "station_id": "BS-%d" % i if k == 0 else ("BS-D" if sq == 9 else "X%d" % sq),
                          "station_seq": sq, "name": "경유%d" % sq, "mobile_no": "0",
                          "lat": 37.3900 + 0.0001 * k, "lng": 126.9502 + 0.0002 * k})
    stops.append({"poi_id": "BS-D", "name": "안양시청", "lat": 37.3900, "lng": 126.9512, "mobile_no": "10095",
                  "routes": dest_routes})
    (poi_dir / "transit_stops.json").write_text(json.dumps(stops, ensure_ascii=False), encoding="utf-8")
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
    with TestClient(m.app) as c:
        c.main = m
        yield c


def _timed_plan(client, **extra):
    t0 = time.perf_counter()
    r = _plan(client, **extra)
    return r, time.perf_counter() - t0


def _count_searches(client, monkeypatch):
    radii = []
    orig = client.main.transit.search

    def spy(*a, **kw):
        radii.append(kw.get("stop_radius_m"))
        return orig(*a, **kw)
    monkeypatch.setattr(client.main.transit, "search", spy)
    return radii


def test_plan_time_is_bounded_when_realtime_times_out(busy_client, monkeypatch):
    """실시간이 건당 0.25초씩 걸리며 전부 실패 — 정류장 수만큼 쌓이지 않고, 확장 탐색도 하지 않는다."""
    live = _FakeLive(delay=0.25, mode="fail")
    busy_client.main.gbis_live.LIVE = live
    radii = _count_searches(busy_client, monkeypatch)
    r, elapsed = _timed_plan(busy_client, realtime=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["low_floor"]["tier"] == 0 and body["low_floor"]["expanded_radius"] is False
    assert body["routes"][0]["summary"]["warnings"][0].startswith("실시간 저상버스 정보를 확인하지 못했습니다")
    n_calls = len(live.arr_calls) + len(live.loc_calls) + len(live.line_calls)
    assert len(live.arr_calls) == gbis_live.CONSECUTIVE_FAIL_LIMIT, live.arr_calls
    assert live.loc_calls == []
    # 실시간 조회 누적은 예산 + 최소 타임아웃을 넘지 않는다 — 조회 수 × 0.25초(정류장 8곳이면 2초 이상)가 아니다
    assert n_calls * 0.25 <= gbis_live.REQUEST_BUDGET_SEC + gbis_live.MIN_CALL_TIMEOUT_SEC
    assert elapsed < 1.5, "계획 소요 %.2fs (실시간 조회 %d회)" % (elapsed, n_calls)
    assert radii == [None], "실시간을 못 받았을 때는 반경 확장 탐색을 하지 않는다: %s" % radii
    bus = _bus(r)
    assert bus["realtime"]["status"] == "unavailable" and bus["realtime"]["items"] == []


def test_plan_time_is_bounded_when_realtime_is_slow_but_alive(busy_client, monkeypatch):
    """응답은 오지만 느린 경우 — 누적 시간이 예산을 넘으면 남은 조회를 생략한다."""
    monkeypatch.setattr(gbis_live, "REQUEST_BUDGET_SEC", 0.5)
    live = _FakeLive(delay=0.2, mode="ok")
    busy_client.main.gbis_live.LIVE = live
    r, elapsed = _timed_plan(busy_client)
    assert r.status_code == 200, r.text
    n_calls = len(live.arr_calls) + len(live.loc_calls) + len(live.line_calls)
    assert n_calls == 3, "0.2초 × 3회에서 예산 0.5초를 넘긴다: %d" % n_calls
    assert elapsed < 1.5, "계획 소요 %.2fs" % elapsed
    assert r.json()["low_floor"]["tier"] == 1, "예산 안에 확인된 저상 후보는 그대로 쓴다"


def test_plan_with_healthy_realtime_queries_every_candidate(busy_client):
    """실시간이 정상이면 예산이 개입하지 않는다 — 후보 정류장을 전부 조회한다."""
    live = _FakeLive(mode="ok")
    busy_client.main.gbis_live.LIVE = live
    r = _plan(busy_client)
    assert r.status_code == 200, r.text
    assert r.json()["low_floor"]["tier"] == 1
    assert len(set(live.arr_calls)) == tp.MAX_CANDIDATE_STOPS
    assert len(live.line_calls) >= 1


def test_no_low_floor_in_service_still_expands_radius(client, monkeypatch):
    """실시간으로 '저상 없음'(tier 3)이 확인된 경우의 반경 확장은 그대로다."""
    client.responses["stationId=BS-FAR"] = _arrival_body("BS-FAR", "R6", "6", (0, 0), (7, 12))
    client.responses["routeId=R6"] = _location_body("R6", [0, 0])
    radii = _count_searches(client, monkeypatch)
    assert _plan(client).json()["low_floor"]["tier"] == 3
    assert radii == [None, low_floor.STOP_RADIUS_EXPANDED_M]


def test_realtime_exception_does_not_break_plan(busy_client):
    """실시간 조회가 예외를 올려도 /route/plan 은 200 — 버스 leg 에는 '실시간 불가'가 붙는다."""
    busy_client.main.gbis_live.LIVE = _FakeLive(mode="raise")
    for extra in ({"realtime": True}, {"realtime": True, "low_floor": False}):
        r = _plan(busy_client, **extra)
        assert r.status_code == 200, r.text
        rt = _bus(r)["realtime"]
        assert rt["status"] == "unavailable" and rt["items"] == [] and rt["next_low_floor"] is None
