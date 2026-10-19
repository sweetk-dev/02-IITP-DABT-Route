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
        n = len(pts)
        for i in range(n):                  # 닫는 변까지 — 첫 점을 끝에 다시 두지 않은 고리도 처리
            if i == n - 1 and pts[0] == pts[-1]:
                break
            d = _seg_dist(0.0, 0.0, *pts[i], *pts[(i + 1) % n])
            best = d if best is None or d < best else best
    for e in exits_for(name):
        d = haversine_m(lat, lng, e["lat"], e["lng"])
        best = d if best is None or d < best else best
    return best


def exit_distance_m(name: str, lat: float, lng: float):
    """출발점에서 가장 가까운 출구까지 거리(m). 출구 자료가 없으면 None."""
    ds = [haversine_m(lat, lng, e["lat"], e["lng"]) for e in exits_for(name)]
    return min(ds) if ds else None


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


def _index_on(line: str, station_name: str):
    """노선 line 의 노선표(LINES)에서 역의 인덱스. 그 노선에 없는 역이면 None.

    _line_of 는 역 이름으로 **처음 맞는 노선**을 고른다. 두 노선이 지나는 역(환승역)이면 다른 노선을
    고를 수 있어, 노선을 이미 아는 곳(지하철 leg 의 line)에서는 이 함수로 그 노선 안에서만 찾는다.
    """
    for i, nm in enumerate(LINES.get(line) or []):
        if station_name and station_name.startswith(nm):
            return i
    return None


def _direction(board_name: str, alight_name: str, line: str = None):
    """(노선, 하차역 인덱스, 진행 방향 north|south) 또는 None.

    line 을 주면 그 노선 안에서만 두 역을 찾는다(v1.35.0) — 환승역에서 다른 노선의 순서로 방향을
    뒤집어 판정하지 않게 한다. 주지 않으면 종전처럼 역 이름으로 노선을 고른다.
    """
    if line:
        ia, ib = _index_on(line, alight_name or ""), _index_on(line, board_name or "")
        if ia is None or ib is None or ia == ib:
            return None
        return line, ia, ("south" if ia > ib else "north")
    line, ia = _line_of(alight_name or "")
    line_b, ib = _line_of(board_name or "")
    if line is None or line != line_b or ia is None or ib is None or ia == ib:
        return None
    return line, ia, ("south" if ia > ib else "north")


def _side_names(line: str, ia: int) -> dict:
    seq = LINES.get(line) or []
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


def _direction_by_travel(station_name: str, travel: str, line: str = None):
    """이용자가 답한 진행 방향(north|south)으로 (노선, 역 인덱스, 진행 방향) — 역 출발 안내(#79).

    line 을 주면 그 노선 안에서 역을 찾는다(v1.35.0, 지하철 leg 의 승강장 판정). 역 안 출발처럼
    노선을 모르면 종전처럼 역 이름으로 노선을 고른다.
    """
    if travel not in TRAVELS:
        return None
    if line:
        ia = _index_on(line, _key(station_name))
        return (line, ia, travel) if ia is not None else None
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
            item = {"kind": kind, "detail_loc": txt, "side": side}
            if kind == "lift":
                # 리프트 크기(DB poi_station_wheelchair_lift.width_mm·length_mm) — 휠체어가 올라갈 수
                # 있는지 이용자가 스스로 판단하도록 그대로 싣는다. 값이 없으면 None(지어내지 않는다).
                item["width_mm"] = f.get("width_mm")
                item["length_mm"] = f.get("length_mm")
            out.append(item)
    return out


# ── 승강장 접근 판정 (v1.35.0) ─────────────────────────────────────────
# 관악역 상행(석수 방향) 승강장에는 승강기가 없고 휠체어리프트(폭 800mm·길이 1,100mm)만 있다.
# 그런데 안양 → 관악(북행) 지하철 경로는 아무 경고 없이 그 승강장에 내리게 안내했다 — 하차 뒤
# 역 안 안내(egress)에서야 "리프트만 있다"고 알려 주어, 이용자는 이미 열차에서 내린 다음에야 알게 된다.
# 그래서 경로를 고르는 단계에서 승차·하차 승강장 쪽의 승강설비를 판정한다. 판정 규칙은 하차 안내
# (egress_guide)와 **같은 함수**(_platform_by_direction)를 쓴다 — 경로 단계에서 "리프트만"이라 했는데
# 역 안 안내에서 "승강기로 이동합니다"라고 하면(또는 그 반대) 이용자는 무엇을 믿어야 할지 모른다.
#
# 승차역 쪽도 같은 함수로 판정한다. DB 문구는 "그 승강장에서 타는 열차의 방향"을 쓰므로
# (예: "석수역 방향 상행승강장"), 북행 열차를 타는 승강장 = 북쪽 이름이 적힌 승강장이다.
# 하차역에서 "내린 곳"도 북행 열차의 승강장이므로, 두 역 모두 진행 방향(travel)으로 같은 판정을 한다.
_TRAVEL_UPDOWN = {"north": "상행", "south": "하행"}

# 리프트만 있는 승강장에서 이용자가 실제로 할 일. 역 전화번호는 설비 자료에 없으므로 번호를 지어 넣지 않는다.
LIFT_ACTION = "승강장의 리프트 호출 버튼을 누르거나 역무실에 연락해 역무원을 불러 주세요"


def subway_travel(board_name: str, alight_name: str, line: str = None):
    """지하철 leg 의 (노선, 진행 방향 north|south). 같은 노선에서 판정이 안 되면 None.

    노선을 함께 돌려주는 이유: 승강장 판정(platform_access)이 역 이름만으로 노선을 다시 고르면
    환승역에서 다른 노선의 설비·방향으로 판정할 수 있다. line 을 주면(플래너가 정한 leg 의 노선)
    그 노선 안에서만 방향을 판정한다.
    """
    d = _direction(_key(board_name), _key(alight_name), line)
    return (d[0], d[2]) if d else None


def _toward_name(line: str, idx: int, travel: str):
    """진행 방향 쪽 바로 다음 역 이름 — "석수 방향"처럼 이용자가 알아듣는 승강장 이름에 쓴다.

    노선표(LINES) 끝 역이면 노선표 바깥 첫 이름(예: 석수 북쪽 → 금천구청)을 쓴다. 없으면 None.
    """
    seq = LINES.get(line)
    if not seq or idx is None or not (0 <= idx < len(seq)):
        return None                       # 노선표에 없는 노선·역 — 방향 이름을 지어내지 않는다
    extra = _LINE_EXTRA.get(line, {"north": [], "south": []})
    if travel == "north":
        return seq[idx - 1] if idx > 0 else (extra["north"][0] if extra["north"] else None)
    return seq[idx + 1] if idx + 1 < len(seq) else (extra["south"][0] if extra["south"] else None)


def lift_size_text(lift) -> str:
    """리프트 크기 문구 — "폭 800mm·길이 1,100mm". 자료에 있는 값만 쓴다(없으면 빈 문자열)."""
    if not isinstance(lift, dict):
        return ""
    parts = []
    for key, label in (("width_mm", "폭"), ("length_mm", "길이")):
        v = lift.get(key)
        try:
            v = int(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            v = None
        if v and v > 0:
            parts.append("%s %smm" % (label, format(v, ",")))
    return "·".join(parts)


def _access_entry(name: str, d, plat: list, side: str):
    """진행 방향 d 의 승강장 접근 판정 한 건.

    access 값(egress_guide 의 문장 선택 순서와 같다):
      · elevator  — 그 승강장 쪽에 승강기가 있다
      · lift_only — 승강기는 없고 휠체어리프트만 있다 (경고·감점 대상)
      · unknown   — 방향을 가릴 수 없는 설비만 있거나 승강장 설비 자료가 없다 (단정하지 않는다)
      · no_info   — 승강장 설비는 있지만 전부 반대편이다 (이 쪽 자료 없음 — 계단뿐인지는 모른다)
    """
    if d is None:
        return None
    arr_elev = [p for p in plat if p["side"] == "arrival" and p["kind"] == "elevator"]
    arr_lift = [p for p in plat if p["side"] == "arrival" and p["kind"] == "lift"]
    unknown = [p for p in plat if p["side"] == "unknown"]
    if arr_elev:
        access = "elevator"
    elif arr_lift:
        access = "lift_only"
    elif unknown or not plat:
        access = "unknown"
    else:
        access = "no_info"
    lift = None
    if access == "lift_only":
        lf = arr_lift[0]
        lift = {"detail_loc": lf["detail_loc"], "width_mm": lf.get("width_mm"),
                "length_mm": lf.get("length_mm")}
    return {"station": name, "side": side, "line": d[0], "travel": d[2],
            "updown": _TRAVEL_UPDOWN.get(d[2]), "toward": _toward_name(d[0], d[1], d[2]),
            "access": access,
            "elevator": arr_elev[0]["detail_loc"] if arr_elev else None,
            "lift": lift}


def platform_access(station_name: str, travel: str, facilities: dict, side: str = "alight",
                    line: str = None):
    """역 하나에서 진행 방향 travel(north|south) 열차의 승강장 접근 판정.

    side 는 "board"(승차) | "alight"(하차) — 문구와 표시에만 쓰이고 판정 규칙은 같다.
    line 은 지하철 leg 의 노선 — 주면 그 노선 안에서 역·방향을 찾는다(환승역 오판 방지).
    노선표에 없는 역이거나 방향을 모르면 None.
    """
    name = _key(station_name)
    d = _direction_by_travel(name, travel, line)
    if d is None:
        return None
    return _access_entry(name, d, _platform_by_direction(d, facilities), side)


def _which_platform(pa: dict) -> str:
    """승강장 이름 — "석수 방향". 다음 역 이름을 모르면 상행/하행."""
    if pa.get("toward"):
        return "%s 방향" % pa["toward"]
    return pa.get("updown") or "진행 방향"


def lift_only_warning(pa: dict) -> str:
    """리프트만 있는 승강장 경고 문장(전체) — 경로 요약·해당 쪽 스텝의 경고와 문장에 쓴다.

    예) 관악역 하차 승강장(석수 방향)에는 대합실로 이어지는 승강기가 없고 휠체어리프트만 있습니다
        (폭 800mm·길이 1,100mm). 승강장의 리프트 호출 버튼을 누르거나 역무실에 연락해 역무원을 불러 주세요.

    "대합실로 이어지는 승강기"라고 쓰는 이유: 같은 하차 스텝에 "2번 출구(승강기)로 나갑니다"가 함께 나온다.
    그냥 "승강기가 없다"고 하면 출구 승강기와 모순처럼 들린다. 없는 것은 **승강장 ↔ 대합실** 승강기이고
    출구 승강기는 별개라는 점이 읽히게 한다. 대합실이 승강장 위(선상역사)든 아래(지하역)든 맞는 표현이다.
    """
    where = "하차" if pa.get("side") == "alight" else "승차"
    size = lift_size_text(pa.get("lift"))
    return ("%s역 %s 승강장(%s)에는 대합실로 이어지는 승강기가 없고 휠체어리프트만 있습니다%s. %s."
            % (pa["station"], where, _which_platform(pa), "(%s)" % size if size else "", LIFT_ACTION))


def lift_only_short(pa: dict) -> str:
    """승차 스텝에서 **내릴 역**의 리프트를 미리 알리는 짧은 문장.

    승차 스텝에 하차 쪽 전체 문장(크기·할 일)까지 붙이면 같은 내용을 하차 때 또 듣게 되고 문장이 길어진다.
    승차 때는 사실만 짧게 말하고, 전체 문장은 하차 스텝과 경로 요약에 둔다.
    예) 내릴 관악역 승강장(석수 방향)에는 휠체어리프트만 있습니다.
    """
    return "내릴 %s역 승강장(%s)에는 휠체어리프트만 있습니다." % (pa["station"], _which_platform(pa))


def egress_guide(station_name: str, board_name: str, facilities: dict, exit_sel: dict,
                 travel: str = None, line: str = None) -> dict:
    """하차 후 안내 — 역 안(승강장) 기준 문장 목록과 역 밖(출구) 기준 문장.

    travel(north|south)을 주면 승차역 대신 그 진행 방향으로 하차 승강장을 판정한다 —
    서비스 밖에서 전철로 와 역 안에서 도보 경로를 시작하는 경우(#79)."""
    name = _key(station_name)
    # 진행 방향 판정 — 역 안 출발(#79)은 이용자가 답한 방향, 지하철 leg 는 승차역 → 하차역 순서로 정한다.
    # 승강장 접근 판정(platform_access)도 같은 d·plat 로 만든다 — 문장과 판정이 어긋나지 않게(v1.35.0).
    # line(지하철 leg 의 노선)을 주면 그 노선 안에서 방향을 판정한다 — 경로 단계 판정과 같은 노선을 쓰게.
    if travel:
        d = _direction_by_travel(name, travel, line)
    else:
        d = _direction(board_name, name, line)
    plat = _platform_by_direction(d, facilities)
    arrival = [p for p in plat if p["side"] == "arrival"]
    unknown = [p for p in plat if p["side"] == "unknown"]
    inside = []
    arr_elev = [p for p in arrival if p["kind"] == "elevator"]
    arr_lift = [p for p in arrival if p["kind"] == "lift"]
    if arr_elev:
        inside.append("내린 승강장의 승강기로 이동합니다 — %s" % arr_elev[0]["detail_loc"])
    elif arr_lift:
        # 리프트만 있는 승강장 (v1.35.0) — 위치만 알려서는 이용자가 무엇을 해야 할지 모른다.
        # 리프트 크기(자료에 있을 때만)와 실제 행동(호출 버튼·역무실 연락)을 함께 말한다.
        lf = arr_lift[0]
        size = lift_size_text(lf)
        inside.append("내린 승강장 쪽에는 휠체어리프트만 있습니다 — %s%s. %s"
                      % (lf["detail_loc"], "(%s)" % size if size else "", LIFT_ACTION))
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
        # 내린 승강장 쪽 접근 판정(v1.35.0) — 화면·음성이 리프트만 있는 승강장을 따로 경고할 때 쓴다
        "platform_access": _access_entry(name, d, plat, "alight"),
        "inside": inside,
        "outside": outside,
        "question": "지금 역 안(승강장)에 계신가요, 역 밖으로 나오셨나요?",
        # 승강장 윤곽 — 클라이언트가 "역을 벗어나 걷기 시작했는지"를 위치로 판단할 때 쓴다(v1.29.1).
        # 승강장 위나 바로 옆의 GPS 는 튀므로 이 윤곽에서 충분히 떨어져야 역 밖으로 본다.
        "area": platforms_for(name),
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
