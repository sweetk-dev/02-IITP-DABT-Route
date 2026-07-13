# -*- coding: utf-8 -*-
"""GPS 좌표 -> 보행 네트워크 노드 스냅.

기존 planning.py 는 노드 ID 를 직접 입력받았다. 실사용에서는 사용자의 현재 위치
(위경도)가 들어오므로, 가장 가까운 통행 가능 노드를 찾아야 한다.
"""
from __future__ import annotations

from .geo import haversine_m
from .profiles import Profile


class SnapError(Exception):
    pass


def _reachable_by_profile(G, node, profile: Profile) -> bool:
    """프로필 기준으로 이 노드에서 나가는 통행 가능 링크가 하나라도 있는가."""
    for _nb, data in G[node].items():
        if data["link_type"] in profile.avoid:
            continue
        if data["slope"] > profile.max_slope_deg:
            continue
        return True
    return False


def snap(store, lat: float, lng: float, profile: Profile = None,
         max_dist_m: float = 300.0, candidates: int = 12, allowed=None) -> dict:
    """최근접 노드 스냅.

    profile 이 주어지면 해당 프로필로 통행 가능한 노드 중에서 고른다.
    (계단으로만 연결된 노드에 스냅되면 경로가 아예 나오지 않는다.)

    allowed 가 주어지면 **그 집합 안에서만** 고른다. 프로필 제약을 적용한 보행망은
    수백 개 조각으로 쪼개지므로(안양 실측 522개), 최대 연결요소를 넘겨 고립된 조각에
    스냅되는 것을 막는다.
    """
    G = store.graph
    node_ids, coords = store.node_index
    if not node_ids:
        raise SnapError("네트워크에 노드가 없습니다")

    scored = []
    for nid, (nlat, nlon) in zip(node_ids, coords):
        d = haversine_m(lat, lng, nlat, nlon)
        scored.append((d, nid, nlat, nlon))
    scored.sort(key=lambda x: x[0])

    best_any = scored[0]

    if allowed:
        # 도달 가능한 덩어리 안에서만 고른다. 후보 수를 제한하지 않는다 —
        # 가까운 노드가 전부 고립 조각이면 조금 더 걸어서라도 갈 수 있는 곳으로 붙여야 한다.
        for d, nid, nlat, nlon in scored:
            if nid in allowed:
                return {
                    "node_id": nid,
                    "snapped": {"lat": nlat, "lng": nlon},
                    "dist_m": round(d, 1),
                    "reachable": d <= max_dist_m,
                    "profile_ok": True,
                    "in_main_component": True,
                }

    if profile is not None:
        for d, nid, nlat, nlon in scored[:candidates]:
            if _reachable_by_profile(G, nid, profile):
                return {
                    "node_id": nid,
                    "snapped": {"lat": nlat, "lng": nlon},
                    "dist_m": round(d, 1),
                    "reachable": d <= max_dist_m,
                    "profile_ok": True,
                }

    d, nid, nlat, nlon = best_any
    return {
        "node_id": nid,
        "snapped": {"lat": nlat, "lng": nlon},
        "dist_m": round(d, 1),
        "reachable": d <= max_dist_m,
        "profile_ok": profile is None,
    }
