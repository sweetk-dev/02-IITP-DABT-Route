# -*- coding: utf-8 -*-
"""지하철역 출구 — 도보 leg 의 시작·끝점과 역 안 이동 안내 (#77).

지금까지 지하철 leg 의 앞뒤 도보는 역 **중심 좌표**에서 계산했다. 출구가 선로 양쪽에
있는 역은 이 방식이 크게 틀린다. 관악역 → 김중업건축박물관은 역 중심에서 2,144m 인데
2번 출구에서는 1,289m 다(2026-09-21 실측). 역사 통로는 보행망에 없으므로, 반대편으로
나가는 경로를 지상에서 우회해 만든다.

여기서는
  · 출구 좌표(OpenStreetMap ``railway=subway_entrance`` 의 출구 번호, station_exits.json)와
  · 출구별 승강기(DB ``poi_station_elevator_unit.exit_no``)를
묶어 "휠체어로 나갈 수 있는 출구" 후보를 만들고, 하차 승강장 쪽 승강기를 골라
역 안 이동 문장을 만든다. 선택(어느 출구가 목적지에 가까운가)은 실제 보행 경로로
호출자(api.main)가 한다.

역 안에서는 위치 측위가 되지 않는다. 그래서 문장을 두 벌 만든다 —
이용자가 아직 **역 안**이면 승강장 승강기 → 출구 승강기 순서를, 이미 **역 밖**이면
출구 기준 안내를 쓴다. 어느 쪽인지는 화면에서 이용자에게 묻는다.
"""
from __future__ import annotations

import json
import os
import re

from ..engine.geo import haversine_m
from .planner import LINES, _line_of

_PATH = os.path.join(os.path.dirname(__file__), "station_exits.json")
_DATA = None


def _load() -> dict:
    global _DATA
    if _DATA is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                _DATA = json.load(f).get("stations", {}) or {}
        except (OSError, ValueError):
            _DATA = {}
    return _DATA


def _key(name: str) -> str:
    n = (name or "").strip()
    return n[:-1] if n.endswith("역") and len(n) > 1 else n


def _exit_no(v):
    """'2', '2번', '4-1' → 출구 번호. '내부'·빈값 → None."""
    m = re.match(r"\s*(\d+(?:-\d+)?)", str(v or ""))
    return m.group(1) if m else None


def exits_for(name: str) -> list:
    return [dict(e) for e in _load().get(_key(name), [])]


def exit_options(name: str, facilities: dict, wheelchair: bool) -> list:
    """출구 후보 목록.

    휠체어 프로필이고 이 역에 출구별 승강기 자료가 있으면 승강기가 있는 출구만 남긴다.
    자료가 없는 역은 전 출구를 후보로 두되 has_elevator=None(모름)으로 표시한다.
    """
    exits = exits_for(name)
    if not exits:
        return []
    elev, lifts = {}, {}
    for e in (facilities or {}).get("elevators") or []:
        no = _exit_no(e.get("exit_no"))
        if no:
            elev.setdefault(no, e.get("detail_loc"))
    for lf in (facilities or {}).get("lifts") or []:
        no = _exit_no(lf.get("exit_no"))
        if no:
            lifts.setdefault(no, lf.get("detail_loc"))
    for ex in exits:
        ex["elevator"] = elev.get(ex["exit_no"])
        ex["lift"] = lifts.get(ex["exit_no"])
        ex["has_elevator"] = (ex["exit_no"] in elev) if elev else None
    if wheelchair and elev:
        with_elev = [e for e in exits if e["exit_no"] in elev]
        if with_elev:
            return with_elev
    return exits


def nearest_exits(opts: list, pt, k: int = 2) -> list:
    """다른 쪽 끝점에서 직선으로 가까운 출구 k 개 — 실제 경로 계산 대상을 줄인다."""
    return sorted(opts, key=lambda e: haversine_m(e["lat"], e["lng"], pt[0], pt[1]))[:k]


def exit_brief(ex: dict) -> dict:
    return {"exit_no": ex["exit_no"], "lat": ex["lat"], "lng": ex["lng"],
            "has_elevator": ex.get("has_elevator"), "elevator": ex.get("elevator"),
            "lift": ex.get("lift")}


def _neighbors(board_name: str, alight_name: str):
    """진행 방향 기준 하차역의 직전 역 이름(반대편 승강장 판정용)."""
    line, ia = _line_of(alight_name or "")
    line_b, ib = _line_of(board_name or "")
    if line is None or line != line_b or ia is None or ib is None or ia == ib:
        return None
    step = 1 if ia > ib else -1
    return LINES[line][ia - step]


def platform_facilities(board_name: str, alight_name: str, facilities: dict) -> list:
    """역 내부(승강장) 승강설비 — 하차 승강장 쪽인지 표시한다.

    DB 문구는 "(1F) 안양역 방향 승강장 …" 처럼 **그 승강장에서 타는 열차의 방향**을 쓴다.
    안양에서 관악으로 왔다면 내린 곳은 '석수 방향' 승강장이다. 그래서 직전 역 이름이
    들어간 "…방향" 설비는 반대편(opposite), 다른 방향 문구는 하차 쪽(arrival), 방향
    문구가 없으면 모름(unknown)으로 둔다. 문구 기반 추정이므로 화면에는 원문을 함께 보인다.
    """
    prev_name = _neighbors(board_name, alight_name)
    out = []
    for kind, key in (("elevator", "elevators"), ("lift", "lifts")):
        for f in (facilities or {}).get(key) or []:
            if _exit_no(f.get("exit_no")):
                continue                      # 출구 설비는 출구 쪽에서 다룬다
            txt = (f.get("detail_loc") or "").strip()
            if not txt:
                continue
            if "방향" not in txt:
                side = "unknown"
            elif prev_name and (prev_name + "역 방향" in txt or prev_name + " 방향" in txt):
                side = "opposite"
            else:
                side = "arrival"
            out.append({"kind": kind, "detail_loc": txt, "side": side})
    return out


def _kind_ko(kind: str) -> str:
    return "승강기" if kind == "elevator" else "휠체어리프트"


def egress_guide(station_name: str, board_name: str, facilities: dict, exit_sel: dict) -> dict:
    """하차 후 안내 — 역 안(승강장) 기준 문장 목록과 역 밖(출구) 기준 문장."""
    name = _key(station_name)
    plat = platform_facilities(board_name, name, facilities)
    arrival = [p for p in plat if p["side"] == "arrival"]
    unknown = [p for p in plat if p["side"] == "unknown"]
    inside = []
    arr_elev = [p for p in arrival if p["kind"] == "elevator"]
    arr_lift = [p for p in arrival if p["kind"] == "lift"]
    if arr_elev:
        inside.append("내린 승강장의 승강기로 이동합니다 — %s" % arr_elev[0]["detail_loc"])
    elif arr_lift:
        inside.append("내린 승강장 쪽에는 휠체어리프트만 있습니다 — %s. 역무원 호출이 필요할 수 있습니다"
                      % arr_lift[0]["detail_loc"])
    elif unknown:
        inside.append("역 안 승강기 위치 — %s" % unknown[0]["detail_loc"])
    elif plat:
        inside.append("내린 승강장 쪽 승강기 정보가 없습니다 — 역무원에게 확인하세요")
    if exit_sel:
        no = exit_sel["exit_no"]
        if exit_sel.get("elevator"):
            inside.append("%s번 출구 승강기로 나갑니다 — %s" % (no, exit_sel["elevator"]))
        elif exit_sel.get("has_elevator") is False and exit_sel.get("lift"):
            inside.append("%s번 출구는 휠체어리프트로 나갑니다 — %s" % (no, exit_sel["lift"]))
        else:
            inside.append("%s번 출구로 나갑니다" % no)
        outside = ("%s역 %s번 출구 앞에서 도보 안내를 시작합니다. 다른 출구로 나오셨다면 "
                   "경로를 다시 찾아 주세요" % (name, no))
    else:
        outside = "%s역 출구 앞에서 도보 안내를 시작합니다" % name
    return {
        "station": name,
        "exit": exit_brief(exit_sel) if exit_sel else None,
        "platform": plat,
        "inside": inside,
        "outside": outside,
        "question": "지금 역 안(승강장)에 계신가요, 역 밖으로 나오셨나요?",
    }
