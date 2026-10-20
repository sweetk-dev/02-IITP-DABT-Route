# -*- coding: utf-8 -*-
"""v1.39.0 — 출구 승강기 위치 · 승차 전 역 안 안내·탑승 칸 · 지하철 구간 노선 선형.

현장 점검(2026-10-16)·실기기 확인(2026-10-20)에서 나온 세 가지:
  1) 관악역 2번·명학역 1번 출구 안내 지점이 승강기가 아니라 계단 출입구였다(OSM 출구 노드).
  2) 관악역 승차 때 승강장으로 가는 승강기·탑승 칸을 안내하지 않았다.
  3) 지하철 구간 지도선이 역 대표점 사이 직선이었다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.transit import exits as ex  # noqa: E402
from route_service.transit import rail  # noqa: E402

GWANAK_FAC = {"elevators": [{"exit_no": "내부", "detail_loc": "(1F) 안양역 방향 승강장 진행방향 앞쪽 끝"},
                            {"exit_no": "2", "detail_loc": "(1F) 2번 출입구 옆"}],
              "lifts": [{"exit_no": "내부", "detail_loc": "1F(석수역 방향 상행승강장 계단 옆)"}]}
MYEONGHAK_FAC = {"elevators": [
    {"exit_no": "내부", "detail_loc": "(1F) 안양역 방향 승강장 1-1 출입문 앞(2F) 1번 출입구 계단 및 맞이방 연결통로"},
    {"exit_no": "내부", "detail_loc": "(1F) 금정역 방향 승강장 10-4 출입문 앞(2F) 2번 출입구 계단 및 맞이방 연결통로"},
    {"exit_no": "1", "detail_loc": "(1F) 1번 출입구 및 성결대학교 셔틀버스 정류장 앞"},
    {"exit_no": "2", "detail_loc": "(1F) 2번 출입구 및 자전거 보관소"}]}


# ── 1) 출구 승강기 위치 ─────────────────────────────────────────────

def test_wheelchair_exit_uses_elevator_position():
    opts = {e["exit_no"]: e for e in ex.exit_options("관악", GWANAK_FAC, wheelchair=True)}
    e2 = opts["2"]
    assert (e2["lat"], e2["lng"]) == (37.418838, 126.90946) and e2["at"] == "elevator"
    assert (e2["stairs_lat"], e2["stairs_lng"]) == (37.4188986, 126.9092237)      # 계단 출입구는 따로 남긴다
    b = ex.exit_brief(e2)
    assert b["at"] == "elevator" and (b["lat"], b["lng"]) == (37.418838, 126.90946)
    m1 = {e["exit_no"]: e for e in ex.exit_options("명학", MYEONGHAK_FAC, wheelchair=True)}["1"]
    assert (m1["lat"], m1["lng"]) == (37.38507, 126.93493)


def test_non_wheelchair_or_no_elevator_keeps_entrance_position():
    e2 = {e["exit_no"]: e for e in ex.exit_options("관악", GWANAK_FAC, wheelchair=False)}["2"]
    assert (e2["lat"], e2["lng"]) == (37.4188986, 126.9092237) and "at" not in e2
    assert "at" not in ex.exit_brief(e2)
    # 출구 승강기 자료가 없으면(has_elevator 거짓·모름) 승강기 좌표를 쓰지 않는다
    e2 = {e["exit_no"]: e for e in ex.exit_options("관악", {}, wheelchair=True)}["2"]
    assert (e2["lat"], e2["lng"]) == (37.4188986, 126.9092237)


# ── 2) 승차 전 역 안 안내 · 탑승 칸 ─────────────────────────────────

def test_car_hint_from_alight_platform_elevator_door():
    pa = ex.platform_access("명학", "south", MYEONGHAK_FAC, "alight")
    h = ex.car_hint(pa, "명학역")
    assert (h["car"], h["door"], h["where"]) == (10, 4, "맨 뒤")
    assert h["text"] == "10번째 칸(맨 뒤)에 타면 명학역 승강기가 가깝습니다"
    pa_n = ex.platform_access("명학", "north", MYEONGHAK_FAC, "alight")
    assert ex.car_hint(pa_n, "명학")["where"] == "맨 앞"                         # 1-1
    # 칸 번호가 없는 문구 · 승강기 없는 승강장 — 추측하지 않는다
    assert ex.car_hint(ex.platform_access("관악", "south", GWANAK_FAC, "alight"), "관악") is None
    assert ex.car_hint(None, "관악") is None


def test_ingress_guide_orders_exit_elevator_gate_platform_car():
    bx = ex.exit_brief({e["exit_no"]: e for e in ex.exit_options("관악", GWANAK_FAC, True)}["2"])
    g = ex.ingress_guide("관악", bx, ex.platform_access("관악", "south", GWANAK_FAC, "board"),
                         ex.platform_access("명학", "south", MYEONGHAK_FAC, "alight"), "명학")
    # v1.41.0 — 관악 2번 출구·안양 방향은 현장 동선(station_exits.json ingress)으로 안내한다
    assert g["inside"][1].startswith("일반 개찰구가 아니라 맞이방 옆 안양역 방면 승강기 전용 개찰구")
    assert g["car"]["car"] == 10
    assert "(1F)" not in g["speech"]
    # 현장 동선이 없는 출구(1번)는 설비 자료로 만든 기존 문장 (v1.40.0)
    bx1 = {"exit_no": "1", "elevator": "(1F) 1번 출입구 옆"}
    g1 = ex.ingress_guide("관악", bx1, ex.platform_access("관악", "south", GWANAK_FAC, "board"),
                          ex.platform_access("명학", "south", MYEONGHAK_FAC, "alight"), "명학")
    assert g1["inside"] == [
        "1번 출구 승강기로 역에 들어갑니다 — (1F) 1번 출입구 옆",
        "개찰구를 지나 안양 방향 승강장 승강기를 탑니다 — (1F) 안양역 방향 승강장 진행방향 앞쪽 끝",
        "10번째 칸(맨 뒤)에 타면 명학역 승강기가 가깝습니다",
    ]
    assert g1["speech"] == "관악역 1번 출구 승강기입니다. 역에 들어가 개찰구를 지나 안양 방향 승강장으로 가세요"
    # 출구 정보가 없으면 출구 문장 없이 승강장·칸만
    g2 = ex.ingress_guide("관악", None, ex.platform_access("관악", "south", GWANAK_FAC, "board"), None, "명학")
    assert len(g2["inside"]) == 1 and g2["car"] is None


def test_board_step_speaks_ingress():
    from route_service.api import main as m
    bx = ex.exit_brief({e["exit_no"]: e for e in ex.exit_options("관악", GWANAK_FAC, True)}["2"])
    leg = {"kind": "subway", "line": "1호선", "station_cnt": 2, "est_distance_m": 4600, "est_duration_sec": 540,
           "board": {"poi_id": "G", "name": "관악", "lat": 37.419578, "lng": 126.908476},
           "alight": {"poi_id": "M", "name": "명학", "lat": 37.384435, "lng": 126.935579},
           "board_exit": bx, "warnings": [],
           "platform_access": {"board": ex.platform_access("관악", "south", GWANAK_FAC, "board"),
                               "alight": ex.platform_access("명학", "south", MYEONGHAK_FAC, "alight")}}
    st = m._transit_step(leg, boarding=True)
    assert st["instruction"] == ("관악역 2번 출구 승강기로 맞이방에 올라가, 옆의 안양역 방면 승강기 전용 개찰구로 들어가세요. "
                                 "승강기에서 내리면 1-1 앞입니다. 1호선 2개 역 뒤 명학역에서 내립니다. "
                                 "승강장 끝 10-4까지 이동해 타면 명학역에서 내리자마자 승강기입니다")   # v1.41.0 현장 동선
    assert st["ingress"]["car"]["car"] == 10
    # v1.40.0 — 승차 안내는 승차 출구(승강기) 앞에서 말하도록 스텝 좌표가 출구다(역 대표점이면 이미 승강장 안)
    assert st["coord"] == [37.418838, 126.90946]
    al = m._transit_step(leg, boarding=False)
    assert "ingress" not in al
    assert al["coord"] == [37.384435, 126.935579]          # 노선 선형이 없는 leg 는 역 대표점
    leg2 = dict(leg, geometry=[[37.4198, 126.9084], [37.3843, 126.9358]], geometry_source="rail_osm")
    assert m._transit_step(leg2, boarding=False)["coord"] == [37.3843, 126.9358]   # 선로 위 하차 승강장 점
    nobx = dict(leg, board_exit=None, platform_access={})
    st3 = m._transit_step(nobx, boarding=True)
    assert st3["coord"] == [37.419578, 126.908476] and st3["instruction"].startswith("관악역에서 1호선에 승차합니다")


# ── 3) 지하철 구간 노선 선형 ─────────────────────────────────────────

def test_rail_segment_follows_line_between_platforms():
    a, b = (37.4197855, 126.9082915), (37.3842645, 126.9356632)     # 관악·명학 승강장 중심
    g = rail.segment("1호선", a, b)
    assert g and len(g) > 5
    L = rail.length_m(g)
    assert 4300 < L < 5000                                            # 직선(4.6km)보다 짧을 수 없고 크게 길지도 않다
    from route_service.engine.geo import haversine_m
    assert haversine_m(g[0][0], g[0][1], *a) < 30 and haversine_m(g[-1][0], g[-1][1], *b) < 30
    assert rail.segment("1호선", b, a) == list(reversed(g))           # 거꾸로 가면 같은 선을 뒤집는다


def test_rail_segment_none_when_off_line_or_unknown():
    assert rail.segment("9호선", (37.41, 126.91), (37.38, 126.93)) is None
    assert rail.segment("1호선", (37.39, 126.99), (37.38, 126.93)) is None   # 선로에서 수 km — 다른 노선·자료 결함


def test_subway_leg_geometry_uses_rail_and_falls_back():
    from route_service.api import main as m
    part = {"line": "1호선", "station_cnt": 2,
            "board": {"poi_id": "G", "name": "관악", "lat": 37.419578, "lng": 126.908476, "elevator_cnt": 3},
            "alight": {"poi_id": "M", "name": "명학", "lat": 37.384435, "lng": 126.935579, "elevator_cnt": 4}}
    leg = m._subway_leg(part)
    assert leg["geometry_source"] == "rail_osm" and len(leg["geometry"]) > 5
    far = dict(part, board={"poi_id": "X", "name": "가상", "lat": 37.39, "lng": 126.99, "elevator_cnt": 1})
    leg2 = m._subway_leg(far)
    assert leg2["geometry_source"] == "straight" and len(leg2["geometry"]) == 2
