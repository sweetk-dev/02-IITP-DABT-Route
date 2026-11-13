# -*- coding: utf-8 -*-
"""역 안내 위치 문구를 말하기용으로 줄인다 (v1.45.0).

설비 자료 문구("(1F) 금정역 방향 승강장 10-4 출입문 앞(2F) 2번 출입구 계단 및 맞이방 연결통로")를 그대로 읽으면
층 표시와 다른 층 설명까지 듣게 된다. 이용자가 승강기를 찾는 층의 설명 하나만 남긴다. 문구는 운영 DB 실제 값.
"""
from route_service.transit import exits as ex

MH_DOWN = "(1F) 금정역 방향 승강장 10-4 출입문 앞(2F) 2번 출입구 계단 및 맞이방 연결통로"
MH_UP = "(1F) 안양역 방향 승강장 1-1 출입문 앞(2F) 1번 출입구 계단 및 맞이방 연결통로"
MH_EXIT1 = "(1F) 1번 출입구 및 성결대학교 셔틀버스 정류장 앞(2F) 1번 출입구 및 맞이방 연결통로"


def test_platform_keeps_only_door():
    assert ex.say_loc(MH_DOWN, "platform") == "10-4 출입문 앞"
    assert ex.say_loc("(1F) 명학역 방향 승강장 7-2 출입문앞", "platform") == "7-2 출입문 앞"
    assert ex.say_loc("(B2) 평촌 방향 승강장 8-1출입문 앞", "platform") == "8-1 출입문 앞"
    assert ex.say_loc("1F(석수역 방향 상행승강장 계단 옆)", "platform") == "계단 옆"


def test_concourse_and_ground_pick_their_floor():
    assert ex.say_loc(MH_UP, "concourse") == "1번 출입구 계단 및 맞이방 연결통로"
    assert ex.say_loc(MH_EXIT1, "concourse", "1") == "맞이방 연결통로"
    assert ex.say_loc(MH_EXIT1, "ground", "1") == "성결대학교 셔틀버스 정류장 앞"
    assert ex.say_loc("(2F) 1번출구 맞이방 서쪽", "concourse", "1") == "맞이방 서쪽"
    # 출구 번호 뒤가 '및·쪽·맞이방'이 아니면 떼지 않는다
    assert ex.say_loc("(B1) 2번 출입구 방향 계단 옆", "concourse", "2") == "2번 출입구 방향 계단 옆"
    # 앞 글("1 3번 해당사항 없음 /")과 '엘리베이터' 낱말은 뺀다
    assert ex.say_loc("1 3번 해당사항 없음 / (1F) 2번 출구 쪽 엘리베이터  2번 출구 옆", "concourse", "2") == "2번 출구 옆"


def test_unreadable_text_is_returned_without_floor_tags():
    assert ex.say_loc("(B1) 4번/5번 출입구 방향", "platform") == "4번/5번 출입구 방향"
    assert ex.say_loc("", "platform") == ""


def test_myeonghak_egress_sentences_are_short():
    fac = {"elevators": [{"exit_no": "내부", "detail_loc": MH_UP}, {"exit_no": "내부", "detail_loc": MH_DOWN},
                         {"exit_no": "1", "detail_loc": MH_EXIT1}], "lifts": []}
    sel = {"exit_no": "1", "lat": 37.38507, "lng": 126.93493, "has_elevator": True, "elevator": MH_EXIT1, "lift": None}
    g = ex.egress_guide("명학", "관악", fac, sel)
    assert g["inside"] == ["내린 승강장의 10-4 출입문 앞 승강기로 이동합니다",
                           "1번 출구 승강기로 나갑니다 — 맞이방 연결통로"]
    assert all("(1F)" not in t and "(2F)" not in t for t in g["inside"])
    # 원문은 판정·탈 칸 계산용으로 그대로 남는다
    assert g["platform_access"]["elevator"] == MH_DOWN


def test_myeonghak_ingress_sentences_are_short():
    fac = {"elevators": [{"exit_no": "내부", "detail_loc": MH_UP}, {"exit_no": "내부", "detail_loc": MH_DOWN},
                         {"exit_no": "1", "detail_loc": MH_EXIT1}], "lifts": []}
    bx = {"exit_no": "1", "elevator": MH_EXIT1}
    g = ex.ingress_guide("명학", bx, ex.platform_access("명학", "north", fac, "board"), None, "관악")
    assert g["inside"][:2] == ["1번 출구 승강기로 역에 들어갑니다 — 성결대학교 셔틀버스 정류장 앞",
                               "개찰구를 지나 안양 방향 승강장 승강기를 탑니다 — 1번 출입구 계단 및 맞이방 연결통로"]
