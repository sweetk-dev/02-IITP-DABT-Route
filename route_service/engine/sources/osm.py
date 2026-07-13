# -*- coding: utf-8 -*-
"""OpenStreetMap 보행 네트워크 -> 표준 그래프.

osmnx 는 선택 의존성이다(그래프 구축 시에만 필요, 서비스 구동 시에는 불필요).
OSM 태그 -> 표준 link_type 매핑:
    highway=steps                      -> steps
    highway=footway + footway=crossing -> crossing
    highway=footway/path/pedestrian    -> sidewalk
    bridge=yes + highway=footway       -> overpass (육교)
    tunnel=yes + highway=footway       -> underpass (지하보도)
    highway=elevator / conveying=yes   -> elevator / ramp
    그 외 보행 허용 도로               -> road
휠체어 관련 태그: wheelchair(yes/limited/no), incline, width, kerb, tactile_paving, surface
"""
from __future__ import annotations

import networkx as nx


def _as_str(v):
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else None
    return None if v is None else str(v)


def _to_float(v):
    s = _as_str(v)
    if not s:
        return None
    s = s.replace("m", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def classify_link(tags: dict) -> str:
    hw = _as_str(tags.get("highway")) or ""
    footway = _as_str(tags.get("footway")) or ""
    bridge = _as_str(tags.get("bridge")) or ""
    tunnel = _as_str(tags.get("tunnel")) or ""
    conveying = _as_str(tags.get("conveying")) or ""

    if hw == "steps":
        if conveying in ("yes", "forward", "backward"):
            return "ramp"  # 에스컬레이터는 휠체어 통행 불가에 준하나 별도 표기
        return "steps"
    if hw == "elevator":
        return "elevator"
    if footway == "crossing" or hw == "crossing":
        return "crossing"
    if hw in ("footway", "path", "pedestrian", "living_street"):
        if bridge and bridge != "no":
            return "overpass"
        if tunnel and tunnel != "no":
            return "underpass"
        return "sidewalk"
    if hw in ("residential", "service", "unclassified", "tertiary", "secondary", "primary"):
        return "road"
    return "unknown"


def incline_to_deg(tags: dict):
    """OSM incline 태그 -> 경사(도). 값이 없거나 up/down 이면 None."""
    raw = _as_str(tags.get("incline"))
    if not raw:
        return None
    raw = raw.strip()
    if raw.endswith("%"):
        try:
            import math

            pct = float(raw[:-1])
            return abs(math.degrees(math.atan(pct / 100.0)))
        except ValueError:
            return None
    try:
        return abs(float(raw))
    except ValueError:
        return None


def build_from_osm(place: str = "Anyang-si, Gyeonggi-do, South Korea",
                   bbox=None, network_type: str = "walk") -> nx.Graph:
    """osmnx 로 보행망을 받아 표준 Graph 를 만든다.

    place 또는 bbox(min_lat, min_lng, max_lat, max_lng) 중 하나를 준다.
    """
    try:
        import osmnx as ox
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "osmnx 가 필요합니다: pip install osmnx  (그래프 구축 시에만 필요)"
        ) from e

    if bbox:
        min_lat, min_lng, max_lat, max_lng = bbox
        Gx = ox.graph_from_bbox(max_lat, min_lat, max_lng, min_lng, network_type=network_type,
                                simplify=True, retain_all=False)
    else:
        Gx = ox.graph_from_place(place, network_type=network_type, simplify=True, retain_all=False)

    G = nx.Graph()
    for nid, d in Gx.nodes(data=True):
        G.add_node(int(nid), lat=float(d["y"]), lon=float(d["x"]), node_type="unknown")

    for u, v, d in Gx.edges(data=True):
        u, v = int(u), int(v)
        if u not in G.nodes or v not in G.nodes:
            continue
        link_type = classify_link(d)
        wheelchair = _as_str(d.get("wheelchair"))
        if wheelchair == "no" and link_type not in ("steps",):
            link_type = "steps"  # 휠체어 통행 불가 표기 -> 회피 대상으로 취급

        geom = None
        if d.get("geometry") is not None:
            try:
                geom = [(float(y), float(x)) for x, y in d["geometry"].coords]
            except Exception:
                geom = None

        length = float(d.get("length") or 0.0)
        slope = incline_to_deg(d) or 0.0
        width = _to_float(d.get("width"))
        kerb = _as_str(d.get("kerb"))
        curb_cut = None
        if kerb in ("lowered", "flush"):
            curb_cut = True
        elif kerb in ("raised",):
            curb_cut = False

        existing = G.get_edge_data(u, v)
        if existing and existing["length"] <= length:
            continue
        G.add_edge(
            u, v,
            length=length,
            slope=slope,
            link_type=link_type,
            width=width,
            curb_cut=curb_cut,
            surface=_as_str(d.get("surface")),
            link_name=_as_str(d.get("name")),
            geometry=geom,
        )
    return G
