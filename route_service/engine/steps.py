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
    "crossing_point": "횡단보도",
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
    out.extend(data.get("report_warnings") or [])   # 이용자 제보 경고 (engine.overrides)
    return out


def _node_crosswalk_step(G, node, position: str = "mid") -> dict | None:
    """노드에 지점 부착된 횡단보도의 안내 스텝(안내 전용 계층).

    안양시 원천 횡단보도 2,728건 중 다수는 crossing 링크가 아니라 최근접 노드에
    지점 메타로 부착돼 있다(apply_city_crosswalks.py [3]단계 -> 노드 crosswalk_cnt).
    경로가 그 노드를 지나면 횡단 안내를 내보낸다. 위상(경로·거리·비용)에는 일절
    관여하지 않으므로 경로 회귀 위험이 없다.

    position: mid(경로 중간 — 횡단 지시) | start·end(출발·도착 지점 — 정보형 안내.
    실제 횡단 여부를 단정할 수 없어 지시형 대신 존재를 알린다).

    cw_curb_cut / cw_tactile_paving 의 None 은 "없음"이 아니라 **미상**이다
    (원천 기재율 4.4%). False 일 때만 "없음" 경고, None 은 "턱낮춤 미상" 표기.
    """
    attrs = G.nodes[node]
    cnt = int(attrs.get("crosswalk_cnt") or 0)
    if cnt <= 0:
        return None
    warnings = []
    curb = attrs.get("cw_curb_cut")
    if curb is False:
        warnings.append("턱낮춤 없음")
    elif curb is None:
        warnings.append("턱낮춤 미상")
    if attrs.get("cw_tactile_paving") is False:
        warnings.append("점자블록 없음")
    many = "" if cnt == 1 else " %d개" % cnt
    if position == "start":
        base = "출발 지점에 횡단보도%s가 있습니다." % many
    elif position == "end":
        base = "도착 지점에 횡단보도%s가 있습니다." % many
    elif cnt == 1:
        base = "횡단보도가 있습니다. 횡단보도를 건너세요."
    else:
        base = "횡단보도 %d개가 있는 지점입니다. 횡단보도를 건너세요." % cnt
    if warnings:
        base += " (%s)" % ", ".join(warnings)
    return {
        "maneuver": "crossing_point",
        "instruction": base,
        "distance_m": 0,
        "duration_sec": 0,
        "coord": [round(float(attrs["lat"]), 7), round(float(attrs["lon"]), 7)],
        "link_type": None,
        "link_name": None,
        "warnings": warnings,
        "crosswalk_cnt": cnt,
    }


def _josa(word: str, with_batchim: str, without_batchim: str) -> str:
    """받침 유무에 따른 조사 선택 — 음성 안내 문장이 어색해지지 않도록."""
    if not word:
        return without_batchim
    ch = word[-1]
    if not ("가" <= ch <= "힣"):
        return without_batchim
    return with_batchim if (ord(ch) - 0xAC00) % 28 else without_batchim


def _sentence(maneuver: str, distance_m: float, link_name, data: dict, warnings: list) -> str:
    dist = int(round(distance_m))
    where = ("%s%s 따라 " % (link_name, _josa(link_name, "을", "를"))) if link_name else ""
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

        # 노드 부착 횡단보도 안내 — 경로 중간 노드(seg 시작점).
        # 앞뒤 어느 한쪽이 crossing 링크면 링크 스텝이 이미 횡단을 안내하므로 생략(중복 방지).
        if i > 0 and lt != "crossing" and raw[i - 1]["data"]["link_type"] != "crossing":
            cw = _node_crosswalk_step(G, seg["u"])
            if cw is not None:
                cw.update({"idx": len(steps), "_cw_point": True,
                           "_link_type": None, "_maneuver_special": True})
                steps.append(cw)

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

        # 출발 지점 부착분 — depart 스텝 뒤에 정보형으로 안내 (2-노드 경로 등에서
        # 부착 노드가 출발점이면 중간 노드 안내만으로는 통째로 침묵하게 된다).
        if i == 0 and lt != "crossing":
            cw = _node_crosswalk_step(G, seg["u"], position="start")
            if cw is not None:
                cw.update({"idx": len(steps), "_cw_point": True,
                           "_link_type": None, "_maneuver_special": True})
                steps.append(cw)
        prev_out = seg["out_bearing"]

    out = []
    for s in steps:
        if s.get("_cw_point"):
            out.append({k: v for k, v in s.items() if not k.startswith("_")})
            continue
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

    # 도착 지점 부착분 — arrive 직전에 정보형으로 안내.
    if raw[-1]["data"]["link_type"] != "crossing":
        cw = _node_crosswalk_step(G, raw[-1]["v"], position="end")
        if cw is not None:
            cw["idx"] = len(out)
            out.append(cw)

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
