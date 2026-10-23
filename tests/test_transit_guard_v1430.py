# -*- coding: utf-8 -*-
"""대중교통 조합 결함 수정 회귀 테스트 (v1.43.0).

2026-10-22 실증 1-A: 관악역 → 김중업건축박물관(직선 785m)에 마을버스 6-2번 58정거장·총 20,161m 경로가
기본으로 나왔다. 원인 두 가지와 마지막 안전장치를 각각 고정한다.

  1) 하차 후보 6곳 제한 — 바로 다음 정류장(목적지에서 8번째로 가까움)이 후보에서 빠졌다
  2) 과다 탑승 — 길 건너 같은 이름 정류장(순환 노선 한 바퀴 뒤)에서 내리는 조합을 걸러내지 못했다
  3) 완성 경로 이상치 점검 — 위 두 규칙을 빠져나가도 상식 밖 경로는 버리고 404(도보 사용)로 끝낸다
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_transit_plan import client, _plan  # noqa: E402,F401  (픽스처 재사용)

from route_service.transit import planner as tp  # noqa: E402

LOOP = {"route_id": "R62", "name": "6-2", "type": "마을버스", "end_station": "순환종점"}

# 실제 좌표(관악역·김중업 일대)를 단순화한 배치
ORIGIN = (37.41906, 126.90867)        # 관악역 2번 출구 부근
TARGET = (37.41883, 126.91760)        # 김중업건축박물관
BOARD = {"poi_id": "B-GWANAK", "name": "관악역", "lat": 37.41992, "lng": 126.90928,
         "routes": [dict(LOOP, station_seq=[8])]}
# 바로 다음 정류장(순번 9) — 목적지에서 411m, 가까운 순 8번째
NEXT_STOP = {"poi_id": "B-ART-9", "name": "안양예술공원", "lat": 37.41653, "lng": 126.91395,
             "routes": [dict(LOOP, station_seq=[9])]}
# 길 건너 같은 이름 정류장(순번 66, 한 바퀴 뒤) — 목적지에서 354m
FAR_SIDE = {"poi_id": "B-ART-66", "name": "안양예술공원", "lat": 37.41667, "lng": 126.91465,
            "routes": [dict(LOOP, station_seq=[66])]}


def _fillers():
    """목적지에 NEXT_STOP 보다 가까운, 이 노선과 무관한 정류장 7곳(가까운 순 1~7위를 채운다)."""
    out = []
    for i in range(7):
        out.append({"poi_id": "F%d" % i, "name": "무관%d" % i,
                    "lat": TARGET[0] - 0.0005 - 0.0001 * i, "lng": TARGET[1] - 0.0005,
                    "routes": [{"route_id": "OTHER%d" % i, "name": "x", "station_seq": [1]}]})
    return out


def _stops_near(stops):
    return lambda la, ln, r: stops


def test_next_stop_beyond_six_nearest_is_candidate():
    """하차 정류장이 목적지에서 8번째로 가까워도 후보가 되어 1정거장 탑승이 선택된다."""
    stops = [BOARD, NEXT_STOP, FAR_SIDE] + _fillers()
    cands = tp.search(ORIGIN, TARGET, "walk_bus", stops_near=_stops_near(stops), stations=[])
    assert cands, "버스 후보가 없다"
    bus = [p for p in cands[0]["parts"] if p["kind"] == "bus"][0]
    assert bus["alight"]["poi_id"] == "B-ART-9", bus["alight"]["poi_id"]
    assert bus["stop_cnt"] == 1


def test_loop_ride_rejected_by_stop_count():
    """길 건너 정류장(58정거장 뒤)만 있으면 과다 탑승으로 버린다 — 후보 없음."""
    stops = [BOARD, FAR_SIDE]
    cands = tp.search(ORIGIN, TARGET, "walk_bus", stops_near=_stops_near(stops), stations=[])
    assert not [c for c in cands if any(p["kind"] == "bus" for p in c["parts"])]


def test_max_ride_stops_rule():
    # 직선 593m → ceil(593/300*2.5)=5 → 최소 8
    assert tp.max_ride_stops(BOARD["lat"], BOARD["lng"], FAR_SIDE["lat"], FAR_SIDE["lng"]) == 8
    # 직선 약 6km → 50정거장까지 허용(정상 장거리 노선은 막지 않는다)
    assert tp.max_ride_stops(37.40, 126.90, 37.454, 126.90) >= 45


def _bus_leg(ride_m, board=BOARD, alight=FAR_SIDE):
    return {"kind": "bus", "route": LOOP, "board": board, "alight": alight,
            "est_distance_m": ride_m}


def _walk(m):
    return {"kind": "walk", "summary": {"total_distance_m": m}}


def test_implausible_bus_ride_detected():
    reason = tp.implausible_reason([_walk(251), _bus_leg(19214), _walk(696)], ORIGIN, TARGET)
    assert reason and "6-2" in reason


def test_plausible_route_passes():
    assert tp.implausible_reason([_walk(251), _bus_leg(560, alight=NEXT_STOP), _walk(500)],
                                 ORIGIN, TARGET) is None


def test_implausible_total_detour_detected():
    # 버스 탑승은 정상이어도 총거리가 직선(785m)의 4배·2km 를 넘으면 버린다
    reason = tp.implausible_reason([_walk(4000), _bus_leg(560, alight=NEXT_STOP)], ORIGIN, TARGET)
    assert reason and "총거리" in reason


def test_api_returns_404_when_all_candidates_implausible(client, monkeypatch):
    """조립된 후보가 전부 이상치면 422 가 아니라 404(대중교통 경로 없음 → 도보 사용)."""
    import route_service.api.main as m
    monkeypatch.setattr(m.transit, "implausible_reason", lambda legs, o, t: "테스트 이상치")
    r = _plan(client, "walk_bus")
    assert r.status_code == 404, r.text
    assert "대중교통" in r.json()["detail"] and "도보" in r.json()["detail"]


def test_api_normal_route_still_ok(client):
    r = _plan(client, "walk_bus")
    assert r.status_code == 200, r.text
