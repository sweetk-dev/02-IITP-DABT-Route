# -*- coding: utf-8 -*-
"""경로 위 횡단보도 위치 목록 (v1.37.0, #118).

길안내 지도에 "어디서 건너는지"를 표시하려면 횡단보도 좌표가 필요하다. 스텝에는 관리번호(crosswalk_id ·
crosswalk_ids)만 있어 화면이 위치를 알 수 없었다(현장 점검 2026-10-16).

목록 항목 {"id", "lat", "lng", "length_m", "role"} — 경로 진행 순서.
  role "cross" : 경로가 건너는 횡단 링크(crossing). 좌표는 링크 선의 가운데. 표시 없는 이면도로 횡단(unmarked)은
                 횡단보도가 아니므로 넣지 않는다.
  role "side"  : 직진으로 지나는 교차로에서 건너야 하는 옆길 횡단보도(스텝 crossing_ahead 의 crosswalk_ids).
  role "near"  : 경로 노드에 붙은 그 밖의 횡단보도(경로 옆에 있는 것 — 건넌다는 뜻이 아니다).
같은 관리번호는 한 번만 싣는다(cross > side > near). 관리번호가 없는 횡단 링크는 id 가 None 이다.
"""
from __future__ import annotations

from .geo import haversine_m
from .graph import edge_coords

_RANK = {"cross": 3, "side": 2, "near": 1}


def _midpoint(coords: list):
    if len(coords) == 1:
        return coords[0]
    seg = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:])]
    half = sum(seg) / 2.0
    for (a, b), d in zip(zip(coords, coords[1:]), seg):
        if half <= d and d > 0:
            t = half / d
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        half -= d
    return coords[-1]


def for_path(G, path: list, steps: list = None) -> list:
    """경로(노드 열)와 그 스텝으로 횡단보도 목록을 만든다. steps 가 없으면 role "side" 없이 cross/near 만."""
    side_ids = set()
    for st in steps or []:
        if st.get("crossing_ahead"):
            side_ids.update(str(x) for x in (st.get("crosswalk_ids") or []) if x)
    items, order = {}, []

    def put(key, item):
        cur = items.get(key)
        if cur is None:
            items[key] = item
            order.append(key)
        elif _RANK[item["role"]] > _RANK[cur["role"]]:
            items[key] = item

    for i, n in enumerate(path):
        for p in G.nodes[n].get("cw_points") or []:
            cid = str(p.get("id") or "")
            if not cid or p.get("lat") is None or p.get("lon") is None:
                continue
            put(cid, {"id": cid, "lat": round(float(p["lat"]), 7), "lng": round(float(p["lon"]), 7),
                      "length_m": (round(float(p["length_m"]), 1) if p.get("length_m") else None),
                      "role": "side" if cid in side_ids else "near"})
        if i + 1 >= len(path):
            break
        v = path[i + 1]
        d = G[n][v]
        if d.get("link_type") != "crossing" or d.get("unmarked"):
            continue
        lat, lng = _midpoint(edge_coords(G, n, v))
        cid = d.get("cw_mgmt_no")
        key = str(cid) if cid else "link:%s:%s" % (n, v)
        length = d.get("cw_length_m") or d.get("length")
        put(key, {"id": (str(cid) if cid else None), "lat": round(lat, 7), "lng": round(lng, 7),
                  "length_m": (round(float(length), 1) if length else None), "role": "cross"})
    return [items[k] for k in order]
