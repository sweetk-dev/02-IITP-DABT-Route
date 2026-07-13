# -*- coding: utf-8 -*-
"""턴바이턴 안내 생성.

경로(노드열)를 사람이 듣고 따라갈 수 있는 문장 단위로 쪼갠다.
음성 안내(로컬 TTS)와 화면 스텝 카드가 같은 문장을 쓴다.
"""
from __future__ import annotations

from .geo import bearing_deg, haversine_m, turn_angle
from .graph import edge_coords
from .profiles import Profile

# 회전 임계각(도)
SLIGHT = 20.0
TURN = 45.0
SHARP = 120.0

MANEUVER_LABEL = {
    "depart": "출발",
    "straight": "직진",
    "slight_left": "좌측 방향",
    "left": "좌회전",
    "sharp_left": "급좌회전",
    "slight_right": "우측 방향",
    "right": "우회전",
    "sharp_right": "급우회전",
    "uturn": "유턴",
    "crossing": "횡단보도",
    "elevator": "승강기",
    "ramp": "경사로",
    "steps": "계단",
    "arrive": "도착",
}


def _maneuver_from_angle(angle: float) -> str:
    a = abs(angle)
    if a < SLIGHT:
        return "straight"
    if a >= 150.0:
        return "uturn"
    if angle > 0:
        if a < TURN:
            return "slight_right"
        return "sharp_right" if a >= SHARP else "right"
    if a < TURN:
        return "slight_left"
    return "sharp_left" if a >= SHARP else "left"


def _edge_warnings(data: dict, profile: Profile) -> list:
    out = []
    slope = float(data["slope"])
    if slope > profile.max_slope_deg:
        out.append("경사 %.1f도 (권장 %.1f도 초과)" % (slope, profile.max_slope_deg))
    elif slope >= profile.max_slope_deg * 0.75 and slope > 0:
        out.append("경사 %.1f도 구간" % slope)
    if data["link_type"] == "crossing" and data.get("curb_cut") is False:
        out.append("턱낮춤 없음")
    if data["link_type"] == "steps":
        out.append("계단 구간")
    w = data.get("width")
    if profile.min_width_m and w is not None and w < profile.min_width_m:
        out.append("보도 폭 %.1fm (좁음)" % w)
    return out


def _sentence(maneuver: str, distance_m: float, link_name, data: dict, warnings: list) -> str:
    dist = int(round(distance_m))
    where = ("%s을 따라 " % link_name) if link_name else ""
    lt = data["link_type"]

    if maneuver == "depart":
        return "%s%dm 앞으로 이동합니다." % (where, dist)
    if maneuver == "arrive":
        return "목적지에 도착했습니다."
    if lt == "crossing":
        base = "횡단보도를 건너 %dm 이동합니다." % dist
    elif lt == "elevator":
        base = "승강기를 이용해 이동합니다."
    elif lt == "ramp":
        base = "경사로를 따라 %dm 이동합니다." % dist
    elif lt == "steps":
        base = "계단 구간 %dm 입니다." % dist
    elif maneuver == "straight":
        base = "%s%dm 직진합니다." % (where, dist)
    else:
        base = "%s 후 %s%dm 이동합니다." % (MANEUVER_LABEL[maneuver], where, dist)

    if warnings:
        base += " (%s)" % ", ".join(warnings)
    return base


def build_steps(G, path, profile: Profile, merge_m: float = 15.0) -> list:
    """노드열 -> 스텝 배열.

    같은 방향(직진)으로 이어지는 짧은 링크는 하나의 스텝으로 합친다.
    링크타입이 바뀌는 지점(횡단보도·승강기·경사로)은 합치지 않는다.
    """
    if len(path) < 2:
        return []

    raw = []
    coord_cursor = 0
    for u, v in zip(path[:-1], path[1:]):
        data = G[u][v]
        coords = edge_coords(G, u, v)
        seg_len = float(data["length"]) or haversine_m(
            coords[0][0], coords[0][1], coords[-1][0], coords[-1][1]
        )
        raw.append(
            {
                "u": u,
                "v": v,
                "data": data,
                "coords": coords,
                "length": seg_len,
                "in_bearing": bearing_deg(coords[0][0], coords[0][1], coords[1][0], coords[1][1]),
                "out_bearing": bearing_deg(
                    coords[-2][0], coords[-2][1], coords[-1][0], coords[-1][1]
                ),
            }
        )

    steps = []
    prev_out = None
    for i, seg in enumerate(raw):
        data = seg["data"]
        lt = data["link_type"]
        if prev_out is None:
            maneuver = "depart"
        else:
            maneuver = _maneuver_from_angle(turn_angle(prev_out, seg["in_bearing"]))
        special = lt in ("crossing", "elevator", "ramp", "steps")

        merge_ok = (
            steps
            and maneuver == "straight"
            and not special
            and steps[-1]["_link_type"] == lt
            and (seg["length"] < merge_m or steps[-1]["_maneuver_special"] is False)
        )
        warnings = _edge_warnings(data, profile)

        if merge_ok:
            last = steps[-1]
            last["distance_m"] += seg["length"]
            last["_coords"].extend(seg["coords"][1:])
            last["_warnings"] = sorted(set(last["_warnings"]) | set(warnings))
            last["_end_node"] = seg["v"]
        else:
            steps.append(
                {
                    "idx": len(steps),
                    "maneuver": lt if special else maneuver,
                    "distance_m": seg["length"],
                    "coord": [round(seg["coords"][0][0], 7), round(seg["coords"][0][1], 7)],
                    "_coords": list(seg["coords"]),
                    "_link_type": lt,
                    "_link_name": data.get("link_name"),
                    "_maneuver_special": special,
                    "_warnings": warnings,
                    "_data": data,
                    "_start_node": seg["u"],
                    "_end_node": seg["v"],
                }
            )
        prev_out = seg["out_bearing"]

    out = []
    for s in steps:
        dist = s["distance_m"]
        instruction = _sentence(
            s["maneuver"] if s["maneuver"] in MANEUVER_LABEL else "straight",
            dist,
            s["_link_name"],
            s["_data"],
            s["_warnings"],
        )
        out.append(
            {
                "idx": s["idx"],
                "maneuver": s["maneuver"],
                "instruction": instruction,
                "distance_m": round(dist),
                "duration_sec": round(dist / profile.speed_mps) if profile.speed_mps else 0,
                "coord": s["coord"],
                "link_type": s["_link_type"],
                "link_name": s["_link_name"],
                "warnings": s["_warnings"],
            }
        )

    last_coord = raw[-1]["coords"][-1]
    out.append(
        {
            "idx": len(out),
            "maneuver": "arrive",
            "instruction": "목적지에 도착했습니다.",
            "distance_m": 0,
            "duration_sec": 0,
            "coord": [round(last_coord[0], 7), round(last_coord[1], 7)],
            "link_type": None,
            "link_name": None,
            "warnings": [],
        }
    )
    return out
