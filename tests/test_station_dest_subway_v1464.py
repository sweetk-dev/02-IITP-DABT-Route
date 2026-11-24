# -*- coding: utf-8 -*-
"""v1.46.4 — 지하철로 역에 가면(목적지가 역) 하차역 출구에서 경로를 끝낸다.

목적지가 역인데 역 대표점까지 도보 구간을 붙이면, 대표점이 철로 건너편 역사 쪽인 역(안양역)에서는 출구로 나온 뒤
철로를 건너는 길을 돌아 1km 넘게 걷는 경로가 나왔다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_exits_category import client  # noqa: E402,F401  (fixture 재사용)

ORIGIN = {"lat": 37.3901, "lng": 126.9501}                      # 합성 보행망 — 안양역 쪽
MH_POINT = {"type": "building", "lat": 37.3908, "lng": 126.9510}   # 명학역 대표점


def _plan(client, **extra):
    body = {"origin": ORIGIN, "destination": MH_POINT, "profile": "wheelchair_electric", "mode": "walk_subway"}
    body.update(extra)
    r = client.post("/route/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_subway_to_station_ends_at_alight_exit(client):
    body = _plan(client, destination_station={"name": "명학역"})
    route = body["routes"][0]
    kinds = [l["kind"] for l in route["legs"]]
    assert kinds[-1] == "subway", kinds                       # 하차 뒤 도보 구간이 없다
    sub = route["legs"][-1]
    ax = sub["alight_exit"]
    no = ax["exit_no"]
    assert sub["alight"]["name"] == "명학" and no
    xy = [round(ax["lat"], 7), round(ax["lng"], 7)]
    steps = route["steps"]
    assert [s["maneuver"] for s in steps[-3:]] == ["subway_alight", "station_exit", "arrive"]
    assert steps[-2]["instruction"] == "명학역 %s번 출구입니다" % no
    assert steps[-1]["instruction"] == "명학역 %s번 출구에 도착했습니다." % no
    assert steps[-1]["coord"] == xy and route["geometry"][-1] == xy
    assert body["destination"]["label"] == "명학역 %s번 출구" % no
    assert body["destination"]["resolved_by"] == "station_exit"
    assert [body["destination"]["lat"], body["destination"]["lng"]] == [ax["lat"], ax["lng"]]
    end = body["station_end"]
    assert end["station"] == "명학" and end["exit"]["exit_no"] == no and end["by"] == "subway"
    walk_after = sum(l["summary"]["total_distance_m"] for l in route["legs"][kinds.index("subway") + 1:]
                     if l["kind"] == "walk")
    assert walk_after == 0


def test_subway_without_station_destination_unchanged(client):
    body = _plan(client)
    route = body["routes"][0]
    assert "station_end" not in body
    assert route["steps"][-1]["instruction"] == "목적지에 도착했습니다."
    assert any(s["maneuver"] == "station_exit" and s["instruction"].endswith("여기서부터 걸어서 이동합니다")
               for s in route["steps"])


def test_other_alight_station_unchanged(client):
    """목적지 역이 아닌 역에서 내리면 그대로 둔다 — 하차역과 목적지 역이 다르다."""
    body = _plan(client, destination_station={"name": "석수"})
    assert "station_end" not in body
    assert body["routes"][0]["steps"][-1]["instruction"] == "목적지에 도착했습니다."


def test_end_station_leg_index_rules():
    import route_service.api.main as m
    sub = {"kind": "subway", "alight": {"name": "안양"}, "alight_exit": {"exit_no": "1", "lat": 1, "lng": 2}}
    walk = {"kind": "walk"}
    assert m._end_station_leg_index([walk, sub, walk], "안양역") == 1
    assert m._end_station_leg_index([walk, sub, walk], "명학") is None
    assert m._end_station_leg_index([walk, dict(sub, alight_exit=None), walk], "안양") is None
    assert m._end_station_leg_index([walk, sub, {"kind": "bus"}, walk], "안양") is None
    assert m._end_station_leg_index([walk], "안양") is None and m._end_station_leg_index([sub], "") is None
