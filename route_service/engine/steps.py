# -*- coding: utf-8 -*-
"""턴바이턴 안내 생성.

경로(노드열)를 사람이 듣고 따라갈 수 있는 문장 단위로 쪼갠다.
음성 안내(로컬 TTS)와 화면 스텝 카드가 같은 문장을 쓴다.
"""
from __future__ import annotations

import math

from .geo import bearing_deg, haversine_m, lead_bearing, trail_bearing, turn_angle
from .graph import edge_coords
from .profiles import Profile

# 짧은 링크의 DEM 경사는 추정치로만 안내한다 (planner.SHORT_LINK_M 과 동일, v1.20.0)
SHORT_LINK_M = 15.0
# 점자블록 안내가 의미 있는 프로필 — 휠체어 안내에 '점자블록 없음'이 붙던 결함 수정 (v1.20.0)
TACTILE_PROFILES = ("visual",)


def tactile_for(profile) -> bool:
    return bool(profile) and getattr(profile, "id", None) in TACTILE_PROFILES

# 직진으로 지나는 교차로에서도 알릴 횡단보도의 기준 (v1.34.0).
# 가는 길을 가로막는 옆길 횡단보도(진행 방향의 좌우로 벗어나 있는 것)이고 건너는 거리가 기준 이상이면 알린다.
CROSS_AHEAD_MIN_M = 9.0           # 건너는 거리(m) — 10m 안팎을 겨냥하되 원천 실측 오차만큼 낮춰 잡는다
CROSS_AHEAD_LATERAL_MIN_M = 5.0   # 진행 축에서 좌우로 이만큼은 벗어나 있어야 옆길 횡단보도로 본다
CROSS_AHEAD_LATERAL_MAX_M = 30.0  # 넓은 길의 반폭까지 — 노드 부착 반경(30m)과 같다
CROSS_AHEAD_ALONG_MAX_M = 15.0    # 진행 방향 앞뒤 허용 폭
CROSS_AHEAD_SPAN_M = 25.0         # 직진 여부·진행 방위각을 재는 앞뒤 구간 — 교차로 안 짧은 절점의 방위각 튐을 넘긴다
CROSS_AHEAD_SIDE_DEG = (30.0, 150.0)   # 옆길로 보는 갈림 각도 범위(진행 방향 기준 좌·우)

# 회전 임계각(도)
SLIGHT = 20.0
TURN = 45.0
SHARP = 120.0

# 방위각을 재는 구간 길이(m) — 링크 끝단 미세 절점의 방위각 튐을 없앤다 (v1.21.0)
BEARING_SPAN_M = 10.0
# 이 길이 미만 링크에서는 회전을 안내하지 않고 각도를 다음 스텝으로 이월한다 (v1.21.0).
# 6m 짜리 모퉁이 절점 하나 때문에 "급좌회전 후 6m" 같은 지시가 나오던 것을 막는다.
# 이월된 각은 다음 회전각과 합산되므로 좌->우로 되꺾이는 지그재그는 서로 상쇄된다.
TURN_MIN_M = 12.0
# 이 길이 미만이면 링크 종류가 달라도 앞 스텝에 흡수한다 (v1.21.0).
SHORT_ABSORB_M = 8.0

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


def _maneuver_from_carried(angle: float) -> str:
    """짧은 링크를 거치며 누적된 회전각(정규화하지 않은 합, 양수=우) → maneuver.

    - 합이 ±180° 이내면 `_maneuver_from_angle` 과 같다(종전 동작).
    - 합의 절댓값이 180° 를 넘으면(같은 쪽으로 연달아 꺾은 경우) ±180° 로 접어 좌우를 뒤집지 않는다.
      예: 우 120° → 8m 링크 → 우 120° 는 합 240° 이고, 접으면 −120°(급좌회전)가 되지만
      이용자는 실제로 오른쪽으로 두 번 돈다. "급좌회전"을 들으면 반대쪽으로 돌게 된다.
      · 접은 값이 유턴 기준(150° 이상)이면 결과적으로 되돌아 나가는 것이므로 유턴(좌우 구분 없음).
      · 그 밖에는 누적된 방향 그대로의 급회전(sharp_right / sharp_left) — 기존 maneuver 체계에서
        "그 방향으로 크게 돈다"를 나타내는 가장 큰 값이다.
    """
    if abs(angle) <= 180.0:
        return _maneuver_from_angle(angle)
    folded = (angle + 180.0) % 360.0 - 180.0
    if abs(folded) >= 150.0:
        return "uturn"
    return "sharp_right" if angle > 0 else "sharp_left"


def _edge_warnings(data: dict, profile: Profile) -> list:
    out = []
    slope = float(data["slope"])
    # 가상 링크(출발·도착 투영으로 잘린 링크)는 원 링크 길이로 본다 — 긴 급경사 링크의 일부를
    # 짧게 지난다고 "짧은 구간 경사 추정"으로 낮춰 말하지 않는다(planner.slope_ref_length 와 같은 기준).
    short = float(data.get("orig_length") or data.get("length") or 0.0) < SHORT_LINK_M
    if slope > profile.max_slope_deg:
        if short:
            out.append("짧은 구간 경사 추정 %.1f도" % slope)   # 격자 보간 오차 가능 — 확정 표현을 피한다 (v1.20.0)
        else:
            out.append("경사 %.1f도 (권장 %.1f도 초과)" % (slope, profile.max_slope_deg))
    elif slope >= profile.max_slope_deg * 0.75 and slope > 0 and not short:
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


def _through_bearing(raw, i, span_m: float = CROSS_AHEAD_SPAN_M):
    """raw[i] 가 시작하는 노드를 지나는 진행 방위각. 직진이 아니면 None (v1.34.0).

    노드 앞뒤 span_m 만큼의 경로 좌표로 잰다. 바로 붙은 링크만 보면 교차로 안 1~2m 절점에서
    방위각이 튀어, 실제로는 직진인데 꺾인 것으로 읽힌다.
    """
    def _walk(coords_iter):
        acc, first, prev = 0.0, None, None
        for c in coords_iter:
            if first is None:
                first = prev = c
                continue
            acc += haversine_m(prev[0], prev[1], c[0], c[1])
            prev = c
            if acc >= span_m:
                break
        return first, prev, acc

    def _back():
        for k in range(i - 1, -1, -1):
            for c in reversed(raw[k]["coords"]):
                yield c

    def _fwd():
        for k in range(i, len(raw)):
            for c in raw[k]["coords"]:
                yield c

    n0, far_b, d_b = _walk(_back())
    n1, far_f, d_f = _walk(_fwd())
    if n0 is None or n1 is None or d_b < 1.0 or d_f < 1.0:
        return None
    b_in = bearing_deg(far_b[0], far_b[1], n0[0], n0[1])
    b_out = bearing_deg(n1[0], n1[1], far_f[0], far_f[1])
    turn = turn_angle(b_in, b_out)
    if abs(turn) >= SLIGHT:
        return None
    return (b_in + turn / 2.0) % 360.0


def _side_roads(G, node, bearing, skip=()) -> set:
    """노드에서 진행 방향의 왼쪽·오른쪽으로 갈리는 길이 있는 쪽 — {"left", "right"} 의 부분집합.

    skip 은 경로가 드나드는 이웃 노드(걸어온 길·갈 길). 횡단보도·승강기 같은 특수 링크와
    길이가 없는 링크(방위각을 잴 수 없다)는 옆길로 치지 않는다.
    """
    lo, hi = CROSS_AHEAD_SIDE_DEG
    out = set()
    for nb in G.neighbors(node):
        if nb in skip:
            continue
        data = G[node][nb]
        if data.get("link_type") in ("crossing", "elevator", "ramp", "steps"):
            continue
        coords = edge_coords(G, node, nb)
        if haversine_m(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1]) < 1.0:
            continue
        rel = turn_angle(bearing, lead_bearing(coords, BEARING_SPAN_M))   # 양수 = 오른쪽
        if lo <= abs(rel) <= hi:
            out.add("right" if rel > 0 else "left")
    return out


def _crosswalks_ahead(G, node, bearing, skip=()) -> list:
    """직진으로 지나는 노드에서, 가는 길을 가로막는 큰 횡단보도를 고른다 (v1.34.0).

    도로 중심선으로 이어진 구간은 옆길을 건너는 횡단이 링크로 잡히지 않아, 노드에 붙은
    횡단보도를 직진 통과 때 모두 생략하면 큰 교차로도 말없이 지나가게 된다.
    노드에 붙은 횡단보도의 위치(cw_points)를 진행 방향 기준으로 나눠 보면
      · 진행 축 위(앞뒤)에 놓인 것 — 지금 따라가는 길을 건너는 횡단보도. 지나칠 뿐이다
      · 진행 축에서 좌우로 벗어난 것 — 옆길을 건너는 횡단보도. 보도로 직진하려면 건너야 한다
    뒤쪽만 골라, 건너는 거리가 CROSS_AHEAD_MIN_M 이상인 것을 돌려준다.
    그쪽으로 실제 갈리는 길이 있을 때만 인정한다 — 나란한 다른 길의 횡단보도가 붙은 경우를 거른다.
    걷는 쪽 보도(왼쪽·오른쪽)는 알 수 없으므로 어느 쪽에 있는지도 함께 준다.
    cw_points 가 없는 그래프에서는 빈 목록 — 종전 동작 그대로다.
    """
    if bearing is None:
        return []
    attrs = G.nodes[node]
    pts = attrs.get("cw_points") or []
    if not pts:
        return []
    roads = _side_roads(G, node, bearing, skip)
    if not roads:                          # 갈림이 없는 지점에는 건널 옆길이 없다
        return []
    lat0, lon0 = float(attrs["lat"]), float(attrs["lon"])
    b = math.radians(float(bearing))
    de, dn = math.sin(b), math.cos(b)      # 진행 방향 단위벡터(동, 북)
    k_e = 111320.0 * math.cos(math.radians(lat0))
    out = []
    for pt in pts:
        try:
            length = float(pt.get("length_m") or 0.0)
            e = (float(pt["lon"]) - lon0) * k_e
            n = (float(pt["lat"]) - lat0) * 110540.0
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if length < CROSS_AHEAD_MIN_M:
            continue
        along = e * de + n * dn
        left = -e * dn + n * de            # 진행 방향의 왼쪽이 +
        if abs(along) > CROSS_AHEAD_ALONG_MAX_M or abs(left) <= abs(along):
            continue
        if not (CROSS_AHEAD_LATERAL_MIN_M <= abs(left) <= CROSS_AHEAD_LATERAL_MAX_M):
            continue
        side = "left" if left > 0 else "right"
        if side not in roads:
            continue
        out.append({"id": pt.get("id"), "side": side, "length_m": length})
    return out


def _node_crosswalk_step(G, node, position: str = "mid", profile=None,
                         turning: bool = False, bearing=None, skip=()) -> dict | None:
    """노드에 지점 부착된 횡단보도의 안내 스텝(안내 전용 계층).

    안양시 원천 횡단보도 2,728건 중 다수는 crossing 링크가 아니라 최근접 노드에
    지점 메타로 부착돼 있다(apply_city_crosswalks.py [3]단계 -> 노드 crosswalk_cnt).
    경로가 그 노드를 지나면 횡단 안내를 내보낸다. 위상(경로·거리·비용)에는 일절
    관여하지 않으므로 경로 회귀 위험이 없다.

    position: mid(경로 중간) | start·end(출발·도착 지점).

    ⚠️ 지시형("건너세요") 금지 — v1.21.0
    노드 부착 횡단보도는 **그 지점에 횡단보도가 있다**는 사실일 뿐, 경로가 그것을
    건넌다는 뜻이 아니다(위상에 관여하지 않는 안내 전용 계층이므로 실제 횡단은
    link_type='crossing' 링크뿐이다). 그런데 종전에는 경로 중간 노드마다
    "횡단보도를 건너세요"라고 지시했고, 안양아트센터→안양문화원 1.4km 경로에서
    **안양로를 직진하는 동안에만 6번** 그 지시가 나왔다(실측 2026-09-04).
    이용자는 "건너세요"를 들으면 횡단보도를 지나치는 게 아니라 실제로 길 건너편으로
    넘어가므로 경로를 이탈한다. 그래서
      · 경로가 그 노드를 **직진 통과**하면 안내하지 않는다(시각 프로필만 정보형 유지 —
        차도 접근 신호로서 값이 있다)
      · 꺾이는 지점이면 정보형("횡단보도가 있는 지점입니다")으로만 알린다
    지시형은 crossing 링크 스텝(_sentence)에서만 쓴다.

    예외 — v1.34.0: 직진 통과라도 가는 길을 가로막는 큰 횡단보도(_crosswalks_ahead)가 있으면
    프로필과 무관하게 알린다. "옆길 횡단보도"라고 짚어, 따라가던 길을 건너라는 뜻으로 듣지
    않게 한다. 한쪽에만 있으면 그쪽 보도로 가는 경우로 한정해 말한다(걷는 쪽을 모른다).
    이 스텝에는 "턱낮춤 미상"을 붙이지 않는다 — 원천 기재율이 낮아 거의 모든 교차로에서
    되풀이되기 때문. 확인된 "없음"만 알린다.
    bearing 은 그 노드를 직진으로 지나는 진행 방위각(직진이 아니면 None).

    cw_curb_cut / cw_tactile_paving 의 None 은 "없음"이 아니라 **미상**이다
    (원천 기재율 4.4%). False 일 때만 "없음" 경고, None 은 "턱낮춤 미상" 표기.
    """
    attrs = G.nodes[node]
    cnt = int(attrs.get("crosswalk_cnt") or 0)
    if cnt <= 0:
        return None
    ahead = _crosswalks_ahead(G, node, bearing, skip) if position == "mid" else []
    if position == "mid" and not turning and not tactile_for(profile) and not ahead:
        return None
    warnings = []
    curb = attrs.get("cw_curb_cut")
    if curb is False:
        warnings.append("턱낮춤 없음")
    elif curb is None and not ahead:
        warnings.append("턱낮춤 미상")
    if attrs.get("cw_tactile_paving") is False and tactile_for(profile):
        warnings.append("점자블록 없음")   # 시각장애 프로필에서만 (v1.20.0)
    many = "" if cnt == 1 else " %d개" % cnt
    if position == "start":
        base = "출발 지점에 횡단보도%s가 있습니다." % many
    elif position == "end":
        base = "도착 지점에 횡단보도%s가 있습니다." % many
    elif ahead:
        sides = {a["side"] for a in ahead}
        if len(sides) == 2:
            base = "교차로입니다. 옆길 횡단보도를 건넙니다."
        else:
            base = ("교차로입니다. %s 보도로 가는 중이면 옆길 횡단보도를 건넙니다."
                    % ("왼쪽" if "left" in sides else "오른쪽"))
    elif cnt == 1:
        base = "횡단보도가 있는 지점입니다."
    else:
        base = "횡단보도 %d개가 있는 지점입니다." % cnt
    if warnings:
        base += " (%s)" % ", ".join(warnings)
    step = {
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
    if ahead:
        sides = {a["side"] for a in ahead}
        step["crossing_ahead"] = True
        step["crossing_side"] = "both" if len(sides) == 2 else next(iter(sides))
        step["crossing_length_m"] = round(max(a["length_m"] for a in ahead))
        step["crosswalk_ids"] = [a["id"] for a in ahead if a.get("id")]
    return step


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
        if data.get("unmarked"):
            # 정제로 이은 이면도로 횡단(#67) — 횡단보도 표시가 없다. 지시형 대신 주의 문구
            base = "이면도로를 건너 %dm 이동합니다. 횡단보도 표시가 없으니 차량을 살피세요." % dist
        else:
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
                "in_bearing": lead_bearing(coords, BEARING_SPAN_M),
                "out_bearing": trail_bearing(coords, BEARING_SPAN_M),
            }
        )

    steps = []
    prev_out = None
    pending_angle = 0.0     # 짧은 링크에서 안내를 미루고 이월 중인 회전각 (v1.21.0)
    for i, seg in enumerate(raw):
        data = seg["data"]
        lt = data["link_type"]
        special = lt in ("crossing", "elevator", "ramp", "steps")
        if prev_out is None:
            maneuver = "depart"
            pending_angle = 0.0
            turn_here = 0.0
        else:
            turn_here = turn_angle(prev_out, seg["in_bearing"])
            pending_angle += turn_here
            # 이월각은 ±180° 로 접지 않고 합 그대로 들고 간다. 짧은 링크를 사이에 두고 같은 쪽으로
            # 두 번 꺾으면 합이 180° 를 넘는데(예: 우 120° + 우 120° = 240°), 접으면 −120° 가 되어
            # 실제로는 오른쪽으로 도는 길이 "급좌회전"으로 안내된다. 판정은 _maneuver_from_carried 가 한다.
            if special:
                # 특수 링크는 링크 종류로 안내한다(종전과 동일). 이월각은 여기서 정리한다.
                maneuver = "straight"
                pending_angle = 0.0
            elif seg["length"] < TURN_MIN_M:
                # 짧은 링크의 회전은 안내하지 않고 다음으로 이월 — 아래 merge 로 앞 스텝에 흡수된다.
                maneuver = "straight"
            else:
                maneuver = _maneuver_from_carried(pending_angle)
                pending_angle = 0.0

        # 노드 부착 횡단보도 안내 — 경로 중간 노드(seg 시작점).
        # 앞뒤 어느 한쪽이 crossing 링크면 링크 스텝이 이미 횡단을 안내하므로 생략(중복 방지).
        if i > 0 and lt != "crossing" and raw[i - 1]["data"]["link_type"] != "crossing":
            # 직진 통과면 알리지 않는다 — 위 _node_crosswalk_step 주석 참고 (v1.21.0)
            # 앞뒤 25m 로 보아 직진이면(through 가 값) 바로 붙은 링크의 방위각 튐은 회전으로 치지 않는다
            through = _through_bearing(raw, i)
            cw = _node_crosswalk_step(G, seg["u"], profile=profile,
                                      turning=(through is None and abs(turn_here) >= SLIGHT),
                                      bearing=through, skip=(raw[i - 1]["u"], seg["v"]))
            if cw is not None:
                cw.update({"idx": len(steps), "_cw_point": True,
                           "_link_type": None, "_maneuver_special": True})
                steps.append(cw)

        merge_ok = (
            steps
            and maneuver == "straight"
            and not special
            # 노드 부착 횡단보도 스텝(_cw_point)은 링크가 아니라 안내 전용이라
            # _coords·_warnings 를 갖지 않는다. 여기에 병합하면 KeyError 로 죽는다.
            # 종전에는 _link_type 이 None 이라 아래 종류 비교에서 자연히 걸러졌는데,
            # v1.21.0 의 짧은 연결부 흡수 조건이 그 비교를 우회해 버렸다.
            and not steps[-1].get("_cw_point")
            # 링크 종류가 같거나, 특수 링크 사이에 낀 아주 짧은 연결부이거나 (v1.21.0).
            # 횡단보도 두 개를 연달아 건너는 교차로에서 그 사이 4m 보도가
            # "4m 직진합니다" 라는 독립 지시로 나오던 것을 앞 스텝에 흡수한다.
            and (steps[-1]["_link_type"] == lt or seg["length"] < SHORT_ABSORB_M)
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
            cw = _node_crosswalk_step(G, seg["u"], position="start", profile=profile)
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
                # 횡단보도 링크의 관리번호(안양시 좌표본) — 한 횡단보도가 여러 링크로 나뉘어 있을 때
                # 클라이언트가 같은 횡단보도의 조각을 묶어 한 번만 안내하게 한다 (v1.33.1, #98)
                "crosswalk_id": (s["_data"].get("cw_mgmt_no") if s["_link_type"] == "crossing" else None),
            }
        )

    # 도착 지점 부착분 — arrive 직전에 정보형으로 안내.
    if raw[-1]["data"]["link_type"] != "crossing":
        cw = _node_crosswalk_step(G, raw[-1]["v"], position="end", profile=profile)
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
