# -*- coding: utf-8 -*-
"""node/link 표(xlsx·csv) -> 표준 그래프.

- 인천 검증본: node.xlsx(node_id, latitude, longitude) / link.xlsx(s_node_id, e_node_id, length, link_name)
- 융기원 요청 규격: NODE_ID/X/Y/NODE_TYPE, LINK_ID/F_NODE/T_NODE/LENGTH/LINK_TYPE(+SLOPE/WIDTH/CURB/SURFACE)

컬럼명은 대소문자·별칭을 흡수한다. LINK_TYPE 한글값도 표준 코드로 매핑한다.
"""
from __future__ import annotations

import networkx as nx

NODE_ALIASES = {
    "node_id": ("node_id", "nodeid", "id"),
    "lat": ("latitude", "lat", "y"),
    "lon": ("longitude", "lon", "lng", "x"),
    "node_type": ("node_type", "nodetype"),
}
LINK_ALIASES = {
    "s_node": ("s_node_id", "f_node", "fnode", "from_node", "start_node"),
    "e_node": ("e_node_id", "t_node", "tnode", "to_node", "end_node"),
    "length": ("length", "len", "distance"),
    "link_type": ("link_type", "linktype", "type"),
    "link_name": ("link_name", "name", "road_name"),
    "slope": ("slope", "incline", "grade"),
    "width": ("width", "eff_width", "road_width"),
    "curb": ("curb", "bump", "kerb", "curb_cut"),
    "surface": ("surface", "pave", "pavement"),
}

LINK_TYPE_MAP = {
    "보도": "sidewalk", "인도": "sidewalk", "sidewalk": "sidewalk", "footway": "sidewalk",
    "이면도로": "road", "도로": "road", "road": "road",
    "횡단보도": "crossing", "crossing": "crossing",
    "계단": "steps", "steps": "steps", "stair": "steps",
    "육교": "overpass", "overpass": "overpass",
    "지하보도": "underpass", "underpass": "underpass",
    "경사로": "ramp", "ramp": "ramp", "slope_way": "ramp",
    "승강기": "elevator", "엘리베이터": "elevator", "elevator": "elevator",
}


def _pick(row_keys, aliases):
    lower = {str(k).strip().lower(): k for k in row_keys}
    for a in aliases:
        if a in lower:
            return lower[a]
    return None


def _norm_link_type(v) -> str:
    if v is None:
        return "unknown"
    s = str(v).strip()
    return LINK_TYPE_MAP.get(s, LINK_TYPE_MAP.get(s.lower(), "unknown"))


def _truthy(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("y", "yes", "true", "1", "있음", "o"):
        return True
    if s in ("n", "no", "false", "0", "없음", "x"):
        return False
    return None


def build_from_tabular(node_path: str, link_path: str) -> nx.Graph:
    import pandas as pd

    rd = (lambda p: pd.read_excel(p)) if str(node_path).endswith((".xlsx", ".xls")) else (
        lambda p: pd.read_csv(p)
    )
    node_df = rd(node_path)
    link_df = (pd.read_excel(link_path) if str(link_path).endswith((".xlsx", ".xls"))
               else pd.read_csv(link_path))

    nc = {k: _pick(node_df.columns, v) for k, v in NODE_ALIASES.items()}
    lc = {k: _pick(link_df.columns, v) for k, v in LINK_ALIASES.items()}
    missing = [k for k in ("node_id", "lat", "lon") if nc[k] is None]
    if missing:
        raise ValueError("node 파일에 필수 컬럼이 없습니다: %s" % missing)
    if lc["s_node"] is None or lc["e_node"] is None:
        raise ValueError("link 파일에 시점/종점 노드 컬럼이 없습니다")

    G = nx.Graph()
    for _, r in node_df.iterrows():
        G.add_node(
            r[nc["node_id"]],
            lat=float(r[nc["lat"]]),
            lon=float(r[nc["lon"]]),
            node_type=(str(r[nc["node_type"]]) if nc["node_type"] else "unknown"),
        )

    for _, r in link_df.iterrows():
        u, v = r[lc["s_node"]], r[lc["e_node"]]
        if u not in G.nodes or v not in G.nodes:
            continue
        length = float(r[lc["length"]]) if lc["length"] else 0.0
        G.add_edge(
            u, v,
            length=length,
            slope=(float(r[lc["slope"]]) if lc["slope"] and r[lc["slope"]] == r[lc["slope"]] else 0.0),
            link_type=_norm_link_type(r[lc["link_type"]] if lc["link_type"] else None),
            width=(float(r[lc["width"]]) if lc["width"] and r[lc["width"]] == r[lc["width"]] else None),
            curb_cut=(_truthy(r[lc["curb"]]) if lc["curb"] else None),
            surface=(str(r[lc["surface"]]) if lc["surface"] else None),
            link_name=(str(r[lc["link_name"]]) if lc["link_name"] else None),
            geometry=None,
        )
    return G
