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
import math
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


_PLATFORMS = None


def _load_platforms() -> dict:
    """역 승강장 윤곽(OSM railway=platform 면). 없는 역은 빈 목록."""
    global _PLATFORMS
    if _PLATFORMS is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                _PLATFORMS = json.load(f).get("platforms", {}) or {}
        except (OSError, ValueError):
            _PLATFORMS = {}
    return _PLATFORMS


def _key(name: str) -> str:
    n = (name or "").strip()
    return n[:-1] if n.endswith("역") and len(n) > 1 else n


def _exit_no(v):
    """'2', '2번', '4-1' → 출구 번호. '내부'·빈값 → None."""
    m = re.match(r"\s*(\d+(?:-\d+)?)", str(v or ""))
    return m.group(1) if m else None


def exits_for(name: str) -> list:
    return [dict(e) for e in _load().get(_key(name), [])]


def platforms_for(name: str) -> list:
    """승강장 윤곽 목록 — 각 원소는 [[lat, lng], ...] 닫힌 고리."""
    return [p.get("ring") or [] for p in _load_platforms().get(_key(name), []) if p.get("ring")]


def _xy(lat0: float, lng0: float, lat: float, lng: float):
    """기준점 둘레 평면 근사(m). 역 하나 크기(수백 m)에서는 오차가 무시할 만하다."""
    r = 6371000.0
    x = math.radians(lng - lng0) * r * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * r
    return x, y


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy)


def _inside(px, py, pts) -> bool:
    c = False
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > py) != (y2 > py) and px < (x2 - x1) * (py - y1) / (y2 - y1) + x1:
            c = not c
    return c


def footprint_distance_m(name: str, lat: float, lng: float):
    """출발점에서 역 시설(승강장 윤곽·출구)까지의 최단 거리(m). 승강장 안이면 0.

    승강장 윤곽 자료가 없는 역은 None — 호출자가 역 중심 기준으로 판정한다.
    (역 중심 좌표는 역사 건물 쪽에 찍혀 있어 승강장에서 60~140m 떨어진 역도 있다 — 명학·관악.)
    """
    rings = platforms_for(name)
    if not rings:
        return None
    best = None
    for ring in rings:
        pts = [_xy(lat, lng, a, b) for a, b in ring]
        if len(pts) >= 3 and _inside(0.0, 0.0, pts):
            return 0.0
        for i in range(len(pts) - 1):
            d = _seg_dist(0.0, 0.0, *pts[i], *pts[i + 1])
            best = d if best is None or d < best else best
    for e in exits_for(name):
        d = haversine_m(lat, lng, e["lat"], e["lng"])
        best = d if best is None or d < best else best
    return best


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
    if wheelchair and not elev and lifts:
        # 승강기는 없고 출구 리프트만 있는 역 — 리프트 출구를 후보로(계단뿐인 출구 배제)
        with_lift = [e for e in exits if e["exit_no"] in lifts]
        if with_lift:
            return with_lift
    return exits


def nearest_exits(opts: list, pt, k: int = 3) -> list:
    """다른 쪽 끝점에서 직선으로 가까운 출구 k 개 — 실제 경로 계산 대상을 줄인다.

    가까운 출구가 보행망에서 막혀 있을 수 있어 3곳까지 본다(승강기 출구는 대개 2~4곳)."""
    return sorted(opts, key=lambda e: haversine_m(e["lat"], e["lng"], pt[0], pt[1]))[:k]


def exit_brief(ex: dict) -> dict:
    return {"exit_no": ex["exit_no"], "lat": ex["lat"], "lng": ex["lng"],
            "has_elevator": ex.get("has_elevator"), "elevator": ex.get("elevator"),
            "lift": ex.get("lift")}


# 노선 방향 판정용 — LINES 순서(북 → 남) 바깥의 역·종착역 이름. 설비 문구는 "○○역 방향",
# "○○ 방면", "상행/하행" 을 섞어 쓰므로, 진행 방향 쪽 이름이 들어 있을 때만 하차 승강장으로 본다.
_LINE_EXTRA = {
    "1호선": {"north": ["금천구청", "구로", "서울", "청량리", "소요산", "광운대", "의정부", "인천", "용산"],
              "south": ["금정", "군포", "의왕", "성균관대", "수원", "병점", "서동탄", "천안", "신창"]},
    "4호선": {"north": ["과천", "사당", "당고개", "진접", "서울역"],
              "south": ["금정", "산본", "수리산", "안산", "오이도"]},
}
# 상행 = 서울 방향(북), 하행 = 반대(남) — 1·4호선 공통
_UPDOWN = {"상행": "north", "하행": "south"}
TRAVELS = ("north", "south")
# 방향 선택지의 먼 쪽 대표 지명 — 이용자가 "어디서 타고 왔는지"를 알아듣는 이름
_FAR = {"1호선": {"north": "서울", "south": "수원"}, "4호선": {"north": "사당", "south": "오이도"}}


def _direction(board_name: str, alight_name: str):
    """(노선, 하차역 인덱스, 진행 방향 north|south) 또는 None."""
    line, ia = _line_of(alight_name or "")
    line_b, ib = _line_of(board_name or "")
    if line is None or line != line_b or ia is None or ib is None or ia == ib:
        return None
    return line, ia, ("south" if ia > ib else "north")


def _side_names(line: str, ia: int) -> dict:
    seq = LINES[line]
    extra = _LINE_EXTRA.get(line, {"north": [], "south": []})
    return {"north": list(seq[:ia]) + extra["north"], "south": list(seq[ia + 1:]) + extra["south"]}


def platform_facilities(board_name: str, alight_name: str, facilities: dict) -> list:
    """역 내부(승강장) 승강설비 — 하차 승강장 쪽인지 표시한다.

    DB 문구는 "(1F) 안양역 방향 승강장 …" 처럼 **그 승강장에서 타는 열차의 방향**을 쓴다.
    안양에서 관악으로 왔다면(북행) 내린 곳은 북쪽으로 가는 열차의 승강장이다. 문구에
    진행 방향 쪽(관악 기준 북쪽: 석수·금천구청·서울…, 또는 '상행') 이름만 있으면 하차 쪽
    (arrival), 반대쪽 이름만 있으면 반대편(opposite), 둘 다 있거나 둘 다 없으면 모름(unknown).
    확실하지 않으면 모름으로 둔다 — 반대편 승강기로 안내하면 휠체어 이용자는 되돌아올 수 없다.
    """
    return _platform_by_direction(_direction(board_name, alight_name), facilities)


def _direction_by_travel(station_name: str, travel: str):
    """이용자가 답한 진행 방향(north|south)으로 (노선, 역 인덱스, 진행 방향) — 역 출발 안내(#79)."""
    if travel not in TRAVELS:
        return None
    line, ia = _line_of(_key(station_name))
    if line is None or ia is None:
        return None
    return line, ia, travel


def _platform_by_direction(d, facilities: dict) -> list:
    names = _side_names(d[0], d[1]) if d else None
    out = []
    for kind, key in (("elevator", "elevators"), ("lift", "lifts")):
        for f in (facilities or {}).get(key) or []:
            if _exit_no(f.get("exit_no")):
                continue                      # 출구 설비는 출구 쪽에서 다룬다
            txt = (f.get("detail_loc") or "").strip()
            if not txt:
                continue
            side = "unknown"
            if d:
                hit = {"north": False, "south": False}
                if "방향" in txt or "방면" in txt:
                    for sd in ("north", "south"):
                        if any(n in txt for n in names[sd]):
                            hit[sd] = True
                for word, sd in _UPDOWN.items():
                    if word in txt:
                        hit[sd] = True
                travel, other = d[2], ("north" if d[2] == "south" else "south")
                if hit[travel] and not hit[other]:
                    side = "arrival"
                elif hit[other] and not hit[travel]:
                    side = "opposite"
            out.append({"kind": kind, "detail_loc": txt, "side": side})
    return out


def _kind_ko(kind: str) -> str:
    return "승강기" if kind == "elevator" else "휠체어리프트"


def egress_guide(station_name: str, board_name: str, facilities: dict, exit_sel: dict,
                 travel: str = None) -> dict:
    """하차 후 안내 — 역 안(승강장) 기준 문장 목록과 역 밖(출구) 기준 문장.

    travel(north|south)을 주면 승차역 대신 그 진행 방향으로 하차 승강장을 판정한다 —
    서비스 밖에서 전철로 와 역 안에서 도보 경로를 시작하는 경우(#79)."""
    name = _key(station_name)
    if travel:
        plat = _platform_by_direction(_direction_by_travel(name, travel), facilities)
    else:
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


def arrival_choices(station_name: str) -> list:
    """역 안에서 출발할 때 "어느 쪽에서 타고 오셨나요?" 선택지 (#79).

    travel 은 **열차의 진행 방향**이다. 서울 쪽에서 타고 왔으면 남쪽으로 달려 온 열차(하행)라
    하행 승강장에 내렸다. 역이 노선표에 없으면 빈 목록 — 방향을 묻지 않는다.
    """
    line, ia = _line_of(_key(station_name))
    if line is None or ia is None:
        return []
    seq = LINES[line]
    extra = _LINE_EXTRA.get(line, {"north": [], "south": []})
    near = {"north": seq[ia - 1] if ia > 0 else (extra["north"][0] if extra["north"] else None),
            "south": seq[ia + 1] if ia + 1 < len(seq) else (extra["south"][0] if extra["south"] else None)}
    out = []
    for came, travel, updown in (("north", "south", "하행"), ("south", "north", "상행")):
        names = [n for n in (near[came], _FAR.get(line, {}).get(came)) if n]
        names = list(dict.fromkeys(names))
        if not names:
            continue
        out.append({"travel": travel, "updown": updown,
                    "label": "%s 쪽에서 타고 왔어요" % "·".join(names)})
    return out


def station_start_guide(station_name: str, travel, facilities: dict, exit_sel: dict) -> dict:
    """역 안(승강장)에서 도보 경로를 시작할 때의 안내 (#79).

    하차 안내(egress_guide)와 같은 문장 규칙을 쓰되, 승차역 대신 이용자가 답한 진행 방향으로
    내린 승강장을 판정한다. 방향을 모르면(travel=None) 승강장 설비를 '모름'으로 두고
    위치만 알린다 — 반대편 승강기로 단정해 안내하지 않는다.
    """
    g = egress_guide(station_name, "", facilities, exit_sel, travel=travel if travel in TRAVELS else None)
    g["travel"] = travel if travel in TRAVELS else None
    g["question"] = None          # 이미 역 안이라고 답했다 — 다시 묻지 않는다
    return g
