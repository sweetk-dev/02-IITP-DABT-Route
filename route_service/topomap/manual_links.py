# -*- coding: utf-8 -*-
"""현장·외부 지도 대조로 확인한 보행 링크를 그래프에 수동으로 얹는다 (#94).

자동 파이프라인(OSM·수치지형도·정제)이 못 잇는 곳 — 선상역사 통로, 역 출구 앞 광장,
지도에 선형이 없는 보도 — 를 JSON 목록으로 관리하고 빌드 시 그래프에 더한다.
오버라이드(engine.overrides)는 기존 링크의 속성만 바꾸므로 **링크 추가**는 이 모듈이 맡는다.

입력 JSON
  {"version": "...", "links": [
     {"id": "GWANAK-01", "from": {"node": 3903318504} | {"lat": .., "lon": .., "snap_m": 5},
      "to": {...}, "link_type": "sidewalk", "link_name": "...", "slope": 0.0,
      "width": null, "geometry": [[lat, lon], ...] (선택), "length": (선택, 없으면 계산),
      "source": "...", "confidence": 0.8, "note": "..."} ] }

끝점 규칙
  node        : 그래프의 기존 노드 ID 그대로 (없으면 그 링크는 건너뛰고 보고서에 남긴다)
  lat/lon     : snap_m(기본 5m) 안에 기존 노드가 있으면 그 노드, 없으면 새 노드 "M<id>_a|_b" 생성.
                앞 항목이 만든 노드에도 스냅되므로 **목록 순서가 결과를 정한다** — 같은 지점을 여러 링크가 쓰면
                그 지점을 만드는 항목을 먼저 둔다. 스냅은 거리만 본다(층·선로 구분 없음) — 반경을 작게 둔다
  geometry    : [[lat, lon], ...] — 이 레포의 링크 geometry 규약(GeoJSON 의 [lon, lat] 이 아니다)
목록은 load_links() 에서 validate_links() 로 검사한다(필수 키·link_type·좌표 범위·id 중복·노드 ID 길이 20자).
링크 속성
  topo_source="manual", manual_id, confidence, source, note — 정제 링크(derived)와 달리
  프로필별 활성 하한(derived_min_confidence)을 받지 않는다. 사람이 확인한 링크이기 때문이다.
같은 두 노드 사이에 이미 링크가 있으면 건너뛴다(exists).
"""
from __future__ import annotations

import json
import math

from ..engine.geo import haversine_m

DEFAULT_SNAP_M = 5.0
_KY = 110_540.0
LINK_TYPES = ("sidewalk", "crossing", "elevator", "ramp", "road", "steps", "overpass", "underpass")
MAX_NODE_ID_LEN = 20          # mv_pednet_node.node_id VARCHAR(20)


class ManualLinkError(ValueError):
    pass


def _check_endpoint(ep, lid: str, side: str):
    if not isinstance(ep, dict):
        raise ManualLinkError("%s.%s: 끝점은 객체여야 한다" % (lid, side))
    if "node" in ep:
        return
    if "lat" not in ep or "lon" not in ep:
        raise ManualLinkError("%s.%s: node 또는 lat/lon 이 필요하다" % (lid, side))
    lat, lon = float(ep["lat"]), float(ep["lon"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ManualLinkError("%s.%s: 좌표 범위 밖 (%s, %s)" % (lid, side, lat, lon))
    if float(ep.get("snap_m", DEFAULT_SNAP_M)) < 0:
        raise ManualLinkError("%s.%s: snap_m 은 0 이상" % (lid, side))


def validate_links(links: list) -> None:
    """빌드 전에 목록을 검사한다 — 필수 키·link_type·좌표 범위·id 중복·노드 ID 길이. 문제가 있으면 ManualLinkError."""
    seen = set()
    for it in links:
        if not isinstance(it, dict) or not it.get("id"):
            raise ManualLinkError("id 없는 항목: %r" % (it,))
        lid = str(it["id"])
        if lid in seen:
            raise ManualLinkError("id 중복: %s" % lid)
        seen.add(lid)
        if len("M%s_a" % lid) > MAX_NODE_ID_LEN:
            raise ManualLinkError("%s: id 가 길어 노드 ID 가 %d자를 넘는다" % (lid, MAX_NODE_ID_LEN))
        for side in ("from", "to"):
            if side not in it:
                raise ManualLinkError("%s: %s 없음" % (lid, side))
            _check_endpoint(it[side], lid, side)
        if it.get("link_type", "sidewalk") not in LINK_TYPES:
            raise ManualLinkError("%s: link_type %r 는 %s 중 하나여야 한다" % (lid, it.get("link_type"), LINK_TYPES))
        for k in ("slope", "width", "length", "confidence"):
            if it.get(k) is not None and float(it[k]) < 0:
                raise ManualLinkError("%s: %s 는 0 이상" % (lid, k))
        if it.get("confidence") is not None and float(it["confidence"]) > 1:
            raise ManualLinkError("%s: confidence 는 0~1" % lid)
        geom = it.get("geometry")
        if geom is not None and (not isinstance(geom, list) or any(len(pt) != 2 for pt in geom)):
            raise ManualLinkError("%s: geometry 는 [[lat, lon], ...]" % lid)


def _node_id(G, lid: str, side: str):
    base = "M%s_%s" % (lid, side)
    nid, i = base, 1
    while nid in G:
        i += 1
        nid = "%s%d" % (base, i)
    return nid


def _nearest(G, lat: float, lon: float, radius_m: float):
    best, best_d = None, radius_m
    kx = 111_320.0 * math.cos(math.radians(lat))
    for n, a in G.nodes(data=True):
        alat, alon = a.get("lat"), a.get("lon")
        if alat is None or alon is None:
            continue
        if abs(alat - lat) * _KY > best_d or abs(alon - lon) * kx > best_d:
            continue
        d = haversine_m(lat, lon, alat, alon)
        if d <= best_d:
            best, best_d = n, d
    return best, best_d


def resolve_endpoint(G, ep: dict, lid: str, side: str):
    """끝점 명세 → (node_id, created: bool, snapped_m|None). 못 찾으면 (None, False, None)."""
    if "node" in ep:
        n = ep["node"]
        if n in G:
            return n, False, None
        if str(n) in G:
            return str(n), False, None
        try:
            if int(n) in G:
                return int(n), False, None
        except (TypeError, ValueError):
            pass
        return None, False, None
    lat, lon = float(ep["lat"]), float(ep["lon"])
    n, d = _nearest(G, lat, lon, float(ep.get("snap_m", DEFAULT_SNAP_M)))
    if n is not None:
        return n, False, round(d, 2)
    nid = _node_id(G, lid, side)
    G.add_node(nid, lat=lat, lon=lon, node_type=ep.get("node_type", "unknown"), manual_id=lid)
    return nid, True, None


def _length(G, u, v, geometry):
    if geometry:
        pts = [(G.nodes[u]["lat"], G.nodes[u]["lon"])] + [tuple(p) for p in geometry] + \
              [(G.nodes[v]["lat"], G.nodes[v]["lon"])]
        return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))
    return haversine_m(G.nodes[u]["lat"], G.nodes[u]["lon"], G.nodes[v]["lat"], G.nodes[v]["lon"])


def load_links(path: str, validate: bool = True) -> list:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    links = list(doc.get("links") or [])
    if validate:
        validate_links(links)
    return links


def apply_manual_links(G, links: list) -> list:
    """링크 목록을 G 에 적용. 반환: [{id, status, u, v, length, created}] (status: added|exists|missing_node)."""
    report = []
    for it in links:
        lid = str(it["id"])
        u, cu, su = resolve_endpoint(G, it["from"], lid, "a")
        if u is None:
            report.append({"id": lid, "status": "missing_node", "u": it["from"].get("node"), "v": None})
            continue
        v, cv, sv = resolve_endpoint(G, it["to"], lid, "b")
        if v is None:
            if cu:
                G.remove_node(u)
            report.append({"id": lid, "status": "missing_node", "u": u, "v": it["to"].get("node")})
            continue
        if u == v or G.has_edge(u, v):
            for n, c in ((u, cu), (v, cv)):        # 이번 항목이 만든 노드는 남기지 않는다
                if c and n in G and G.degree(n) == 0:
                    G.remove_node(n)
            report.append({"id": lid, "status": "exists", "u": u, "v": v})
            continue
        geometry = it.get("geometry") or None
        length = float(it["length"]) if it.get("length") is not None else _length(G, u, v, geometry)
        length = round(max(length, 0.5), 2)
        G.add_edge(u, v, length=length, slope=float(it.get("slope") or 0.0),
                   link_type=it.get("link_type", "sidewalk"), width=it.get("width"),
                   curb_cut=it.get("curb_cut"), tactile_paving=it.get("tactile_paving"),
                   surface=it.get("surface"), link_name=it.get("link_name"),
                   geometry=[tuple(p) for p in geometry] if geometry else None,
                   topo_source="manual", manual_id=lid,
                   confidence=float(it.get("confidence", 0.8)),
                   source=it.get("source"), note=it.get("note"))
        report.append({"id": lid, "status": "added", "u": u, "v": v, "length": length,
                       "created": [n for n, c in ((u, cu), (v, cv)) if c],
                       "snapped_m": [s for s in (su, sv) if s is not None]})
    return report
