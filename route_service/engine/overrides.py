# -*- coding: utf-8 -*-
"""그래프 오버라이드 적용 — 제보·실측으로 얻은 보정을 좌표 앵커로 그래프에 얹는다.

오버라이드는 노드/링크 ID 가 아니라 **좌표+반경**으로 저장된다(mv_access_override).
그래프를 재생성해도 로드 시점에 다시 적용되므로 보정이 유실되지 않는다.

적용 규칙
  warning        : 링크 attr `report_warnings`(list) 에 문구 추가 — 안내·요약에 노출
  curb_cut       : bool 설정 ('true'/'false') — planner.edge_passable 차단 규칙 그대로 작동
  tactile_paving : bool 설정
  width          : float 설정 (m)
  passable       : 'false' 면 링크 attr `blocked=True` — 라우팅에서 제외

`apply_overrides` 는 **재적용 가능(idempotent)** 하다: 이전 적용분을 먼저 되돌린 뒤
새로 적용한다. 원본 값은 링크 attr `_ov_orig` 에 보관한다.
"""
from __future__ import annotations

from .geo import haversine_m, point_segment_dist_m

_BOOL = {"true": True, "false": False, "1": True, "0": False}


def _revert(G) -> int:
    n = 0
    for _u, _v, d in G.edges(data=True):
        if "report_warnings" in d:
            del d["report_warnings"]
        if "blocked" in d:
            del d["blocked"]
        orig = d.pop("_ov_orig", None)
        if orig:
            d.update(orig)
            n += 1
    return n


def _nearest_edge(G, lat: float, lon: float, radius_m: float):
    """좌표에서 radius 안의 최근접 링크 (u, v, data) — 없으면 None."""
    best, best_d = None, radius_m
    for u, v, d in G.edges(data=True):
        du = G.nodes[u]
        dv = G.nodes[v]
        # 대략 필터: 양끝이 모두 radius+링크길이 밖이면 스킵 (계산량 절감)
        approx = min(haversine_m(lat, lon, du["lat"], du["lon"]),
                     haversine_m(lat, lon, dv["lat"], dv["lon"]))
        if approx > best_d + float(d.get("length") or 0):
            continue
        dist = point_segment_dist_m(lat, lon, du["lat"], du["lon"], dv["lat"], dv["lon"])
        if dist <= best_d:
            best, best_d = (u, v, d), dist
    return best


def apply_overrides(G, overrides: list) -> dict:
    """오버라이드 목록을 그래프에 적용. 반환: 적용 통계."""
    reverted = _revert(G)
    stat = {"reverted": reverted, "applied": 0, "unmatched": 0, "warnings": 0,
            "attrs": 0, "blocked": 0}
    for ov in overrides:
        lat, lon = float(ov["lat"]), float(ov["lon"])
        radius = float(ov.get("radius_m") or 20.0)
        attr, value = ov["attr"], str(ov["value"])
        hit = _nearest_edge(G, lat, lon, radius)
        if hit is None:
            stat["unmatched"] += 1
            continue
        _u, _v, d = hit
        if attr == "warning":
            d.setdefault("report_warnings", [])
            if value not in d["report_warnings"]:
                d["report_warnings"].append(value)
            stat["warnings"] += 1
        elif attr == "passable":
            if _BOOL.get(value.lower()) is False:
                d.setdefault("_ov_orig", {})
                d["blocked"] = True
                stat["blocked"] += 1
        elif attr in ("curb_cut", "tactile_paving"):
            b = _BOOL.get(value.lower())
            if b is None:
                stat["unmatched"] += 1
                continue
            d.setdefault("_ov_orig", {}).setdefault(attr, d.get(attr))
            d[attr] = b
            stat["attrs"] += 1
        elif attr == "width":
            try:
                w = float(value)
            except ValueError:
                stat["unmatched"] += 1
                continue
            d.setdefault("_ov_orig", {}).setdefault("width", d.get("width"))
            d["width"] = w
            stat["attrs"] += 1
        stat["applied"] += 1
    return stat
