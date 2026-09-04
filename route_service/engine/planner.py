# -*- coding: utf-8 -*-
"""경로 탐색.

기존 planning.py 대비 달라진 점
  1) slope < 4.0 고정 필터 -> 프로필별 하드 필터(경사·회피 링크타입·보도폭)
  2) weight=length -> 경사·링크타입 가중 비용 (계산만 하고 안 쓰던 weight 를 실제로 사용)
  3) 경로 없음 시 즉시 실패 -> 제약 단계적 완화 후 재시도(fallback 표기)
  4) 노드열만 반환 -> 좌표열·구간 속성·요약 지표 반환
"""
from __future__ import annotations

import uuid

import networkx as nx

from .geo import (haversine_m, lead_bearing, path_length_m,
                  point_segment_dist_m, trail_bearing, turn_angle)
from .graph import edge_coords
from .profiles import Profile

# 유턴 판정 각도 — steps.py 의 턴바이턴 유턴 기준(150도)과 동일해야
# "안내에는 유턴으로 뜨는데 탐색은 못 잡는" 불일치가 안 생긴다.
UTURN_ANGLE_DEG = 150.0
# 유턴 검출용 방위각 측정 구간(m) — steps.BEARING_SPAN_M 과 같은 취지 (v1.21.0)
UTURN_BEARING_SPAN_M = 10.0
UTURN_RETRY = 3
# 짧은 링크(횡단보도 8m 등)의 DEM 경사는 5m 격자 보간 오차가 그대로 각도로 튄다(8m 링크 1.1m 차이 = 7.8°).
# 이 길이 미만은 경사로 통행을 막지 않고 비용 가중만 한다 (v1.20.0).
SHORT_LINK_M = 15.0


class NoRouteError(Exception):
    pass


def _uturn_edges(G, path) -> set:
    """경로에서 유턴(150도 이상 회차)이 발생하는 지점의 앞뒤 링크 집합.

    노드 기반 A* 는 회전 비용을 모른다. 그 결과 지하차도 진입로를 타고 들어갔다가
    되돌아 나오는 식의 경로(#27, 일번가지하차도 583m + 유턴)가 최단으로 뽑힌다.
    탐색 후 기하로 유턴을 검출해 해당 링크에 페널티를 주고 재탐색하는 방식으로 억제한다.
    """
    edges = set()
    prev_out = None
    prev_edge = None
    for u, v in zip(path[:-1], path[1:]):
        coords = edge_coords(G, u, v)
        # 방위각은 끝단 두 점이 아니라 진행 구간으로 잰다 — 미세 절점 때문에 멀쩡한
        # 링크가 유턴으로 오검출돼 페널티를 받고 오히려 우회가 나오던 것을 막는다 (v1.21.0)
        in_b = lead_bearing(coords, UTURN_BEARING_SPAN_M)
        out_b = trail_bearing(coords, UTURN_BEARING_SPAN_M)
        if prev_out is not None and abs(turn_angle(prev_out, in_b)) >= UTURN_ANGLE_DEG:
            edges.add(prev_edge)
            edges.add(frozenset((u, v)))
        prev_out = out_b
        prev_edge = frozenset((u, v))
    return edges


def edge_passable(data: dict, profile: Profile, max_slope_deg: float) -> bool:
    if data.get("blocked"):
        # 제보·실측 오버라이드(passable=false, engine.overrides) — 승인제로만 설정된다
        return False
    if data["link_type"] in profile.avoid:
        return False
    # max_slope_deg 는 하드 상한(profile.hard_slope() 또는 완화 단계). 짧은 링크는 경사로 막지 않는다 (v1.20.0)
    if data["slope"] > max_slope_deg and float(data.get("length") or 0.0) >= SHORT_LINK_M:
        return False
    w = data.get("width")
    if profile.min_width_m and w is not None and w < profile.min_width_m:
        return False
    if profile.requires_curb_cut and data["link_type"] == "crossing":
        if data.get("curb_cut") is False:
            return False
    return True


def edge_cost(data: dict, profile: Profile, penalty: float = 1.0) -> float:
    length = max(float(data["length"]), 0.1)
    slope = float(data["slope"])
    cost = length * (1.0 + profile.slope_factor * slope)
    over = slope - float(profile.max_slope_deg)
    if over > 0:
        # 권장 초과 구간은 우회로가 있으면 피하되, 우회가 몇 배로 길어지면 그냥 지난다 (v1.20.0)
        cost *= 1.0 + float(getattr(profile, "slope_over_penalty", 1.0)) * over
    cost *= profile.penalize.get(data["link_type"], 1.0)
    return cost * penalty


def _astar(G, start, goal, profile, max_slope_deg, penalized_edges=None):
    penalized_edges = penalized_edges or set()

    def weight(u, v, data):
        if not edge_passable(data, profile, max_slope_deg):
            return None  # networkx: None = 통행 불가
        pen = 2.5 if (frozenset((u, v)) in penalized_edges) else 1.0
        return edge_cost(data, profile, pen)

    def heuristic(u, v):
        # 최소 비용 하한(직선거리) — 가중치가 length 이상이므로 admissible
        return haversine_m(
            G.nodes[u]["lat"], G.nodes[u]["lon"], G.nodes[v]["lat"], G.nodes[v]["lon"]
        )

    return nx.astar_path(G, start, goal, heuristic=heuristic, weight=weight)


def _summarize(G, path, profile, slope_coverage: float = 1.0) -> dict:
    dist = 0.0
    slopes = []
    counts = {"steps": 0, "crossing": 0, "elevator": 0, "ramp": 0}
    warnings = []
    for u, v in zip(path[:-1], path[1:]):
        d = G[u][v]
        dist += float(d["length"]) or haversine_m(
            G.nodes[u]["lat"], G.nodes[u]["lon"], G.nodes[v]["lat"], G.nodes[v]["lon"]
        )
        slopes.append(float(d["slope"]))
        lt = d["link_type"]
        if lt in counts:
            counts[lt] += 1
        if lt == "crossing" and d.get("curb_cut") is False:
            warnings.append("턱낮춤 없는 횡단보도 구간이 있습니다")
        warnings.extend(d.get("report_warnings") or [])   # 이용자 제보 경고 (overrides)
        if float(d["slope"]) > profile.max_slope_deg and float(d.get("length") or 0.0) >= SHORT_LINK_M:
            warnings.append(
                "권장 경사(%.1f도)를 넘는 구간이 포함되어 있습니다" % profile.max_slope_deg
            )
    # 노드 지점 부착 횡단보도(안내 전용 계층) — crossing 링크와 별개로 집계한다.
    # 기존 crossing_cnt(= crossing 링크 수)는 클라이언트 계약이 있으므로 의미를 바꾸지 않는다.
    cw_points = 0
    for n in path:
        c = int(G.nodes[n].get("crosswalk_cnt") or 0)
        if c:
            cw_points += c
            if G.nodes[n].get("cw_curb_cut") is False:
                warnings.append("턱낮춤 없는 횡단보도 구간이 있습니다")

    mean_slope = sum(slopes) / len(slopes) if slopes else 0.0
    max_slope = max(slopes) if slopes else 0.0
    duration = dist / profile.speed_mps if profile.speed_mps else 0.0

    # 접근성 점수: 경사 여유 + 계단/무턱낮춤 감점 (0~1)
    slope_score = max(0.0, 1.0 - (max_slope / max(profile.max_slope_deg, 0.1)))
    penalty = 0.3 * counts["steps"] + 0.05 * len(set(warnings))
    score = max(0.0, min(1.0, 0.6 * slope_score + 0.4 - penalty))

    # 경사 데이터가 없는 네트워크에서 "경사 0 = 만점"은 거짓 안심을 준다.
    # 점수를 깎고 사실을 경고로 알린다.
    if slope_coverage < 0.5:
        warnings.append("경사 데이터가 없어 경사 회피가 적용되지 않았습니다")
        score = min(score, 0.6)

    return {
        "total_distance_m": round(dist),
        "duration_sec": round(duration),
        "mean_slope_deg": round(mean_slope, 2),
        "max_slope_deg": round(max_slope, 2),
        "stairs_cnt": counts["steps"],
        "crossing_cnt": counts["crossing"],
        "crossing_point_cnt": cw_points,
        "elevator_cnt": counts["elevator"],
        "ramp_cnt": counts["ramp"],
        "accessibility_score": round(score, 2),
        "warnings": sorted(set(warnings)),
    }


def _geometry(G, path) -> list:
    coords = []
    for u, v in zip(path[:-1], path[1:]):
        seg = edge_coords(G, u, v)
        if coords and coords[-1] == seg[0]:
            seg = seg[1:]
        coords.extend(seg)
    if not coords and path:
        n = path[0]
        coords = [(G.nodes[n]["lat"], G.nodes[n]["lon"])]
    return [[round(a, 7), round(b, 7)] for a, b in coords]


def plan(store, start_node, goal_node, profile: Profile, alternatives: int = 1,
         relax: bool = True) -> dict:
    """경로 탐색 + 대안 경로.

    반환: {"routes": [...], "fallback": {...}}
    """
    G = store.graph
    if start_node == goal_node:
        raise NoRouteError("출발지와 목적지가 같은 지점입니다")

    hard = profile.hard_slope()   # 하드 상한 (v1.20.0) — 권장 상한(max_slope_deg) 초과는 가중으로 처리한다
    fallback = {"used": False, "reason": None, "applied_max_slope_deg": hard}
    levels = [hard]
    if relax:
        levels += [hard + 2.0, hard + 4.0]

    primary = None
    for i, lvl in enumerate(levels):
        try:
            primary = _astar(G, start_node, goal_node, profile, lvl)
            if i > 0:
                fallback = {
                    "used": True,
                    "reason": (
                        "제약(최대 경사 %.1f도)을 만족하는 경로가 없어 %.1f도까지 완화해 탐색했습니다"
                        % (hard, lvl)
                    ),
                    "applied_max_slope_deg": lvl,
                }
            break
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue

    if primary is None:
        raise NoRouteError("통행 가능한 경로를 찾지 못했습니다")

    applied = fallback["applied_max_slope_deg"]

    # 유턴 억제 재탐색 — 유턴 없는 경로가 나오면 채택, 끝까지 없으면 유턴 최소 경로.
    uturn_pen = set()
    best_n, best_path = len(_uturn_edges(G, primary)), primary
    for _ in range(UTURN_RETRY):
        if best_n == 0:
            break
        uturn_pen |= _uturn_edges(G, best_path)
        try:
            cand = _astar(G, start_node, goal_node, profile, applied, uturn_pen)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            break
        if cand == best_path:
            break
        n = len(_uturn_edges(G, cand))
        if n < best_n:
            best_n, best_path = n, cand
    primary = best_path

    routes = [primary]

    # 대안 경로: 1안의 링크에 페널티를 주고 재탐색 (중복 경로는 버림)
    penalized = set()
    for _ in range(max(0, alternatives - 1)):
        penalized |= {frozenset((u, v)) for u, v in zip(routes[-1][:-1], routes[-1][1:])}
        try:
            alt = _astar(G, start_node, goal_node, profile, applied, penalized)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            break
        if any(alt == r for r in routes):
            break
        routes.append(alt)

    coverage = float(store.meta.get("slope_coverage", 1.0) or 0.0)
    out = []
    for path in routes:
        out.append(
            {
                "path": path,
                "summary": _summarize(G, path, profile, coverage),
                "geometry": _geometry(G, path),
            }
        )
    return {"routes": out, "fallback": fallback}


def off_route_distance_m(geometry, lat: float, lng: float) -> float:
    """현재 위치와 경로선 사이의 최단거리(m) — 이탈 판정용."""
    if not geometry:
        return float("inf")
    if len(geometry) == 1:
        return haversine_m(lat, lng, geometry[0][0], geometry[0][1])
    best = float("inf")
    for (alat, alon), (blat, blon) in zip(geometry[:-1], geometry[1:]):
        d = point_segment_dist_m(lat, lng, alat, alon, blat, blon)
        if d < best:
            best = d
    return best
