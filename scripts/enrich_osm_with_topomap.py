# -*- coding: utf-8 -*-
"""OSM 보행 그래프에 수치지형도 보도 속성(폭·재질)을 보강한다.

전면 재검토(2026-07-16) 결론에 따른 하이브리드 구조:
  - 연결 골격 = OSM 보행망 (횡단보도 포함, 위상 연결 보장)
  - 속성 원천 = 수치지형도 인도 면형 (1:1,000 2022 우선, 1:5,000 2025 폴백)

근거(실측):
  - 수치지형도 단독 그래프는 횡단보도 부재로 최대 연결요소 4.6% — 경로탐색 불가
  - 1:5,000(2025) 는 전 시가지 보도 커버 + 폭 100% 기재, 1:1,000(2022) 는 세밀하나 커버 구멍
  - 두 연도(2022/2025) 보도망 형상은 중첩 구역에서 사실상 동일 — 연도 차이 실무 영향 미미

사용:
  python scripts/enrich_osm_with_topomap.py \
      --graph data/network_anyang.gpickle \
      --src-1k  "../91-조사설계_이동편의/경기도 안양시 지도/안양시 수치지형도/1_1000_2026-07-16" \
      --src-5k  "../91-조사설계_이동편의/경기도 안양시 지도/안양시 수치지형도" \
      --out data/network_anyang_enriched.gpickle
"""
from __future__ import annotations

import argparse
import glob
import os
import pickle
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MATCH_RADIUS = 8.0    # m — OSM 링크 표본점에서 보도 폴리곤을 찾는 반경


def _read_sidewalk_polys(zip_path: str, prefixes=("N1A_", "N3A_", "")):
    """도엽 zip 에서 인도 면형(A0033320) 폴리곤·속성을 읽는다 (N1/N3/평문형 모두)."""
    import shapefile
    out = []
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return out
    with tempfile.TemporaryDirectory() as td:
        zf.extractall(td)
        for pre in prefixes:
            p = os.path.join(td, f"{pre}A0033320.shp")
            if not os.path.exists(p):
                continue
            try:
                r = shapefile.Reader(p, encoding="cp949", encodingErrors="replace")
            except Exception:
                continue
            fl = [x[0] for x in r.fields[1:]]
            wi = fl.index("폭") if "폭" in fl else None
            si = fl.index("재질") if "재질" in fl else None
            items = list(zip(r.shapes(), r.records()))
            r.close()
            for sh, rec in items:
                w = rec[wi] if wi is not None else None
                s = rec[si] if si is not None else None
                out.append((sh.points, sh.parts,
                            float(w) if isinstance(w, (int, float)) and w > 0 else None,
                            str(s).strip() or None if s else None))
            break
    return out


def _load_polys(src: str, digits: int):
    from route_service.topomap.dissolve import _to_polygon
    polys, attrs, seen = [], [], set()
    pat = re.compile(r"_(\d{%d})_" % digits)
    for z in sorted(glob.glob(os.path.join(src, "**", "*.zip"), recursive=True)):
        m = pat.search(os.path.basename(z))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        for points, parts, w, s in _read_sidewalk_polys(z):
            g = _to_polygon(points, parts)
            if g is None:
                continue
            polys.append(g)
            attrs.append({"width": w, "surface": s})
    return polys, attrs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--src-1k", required=True, help="1:1,000 도엽 폴더 (9자리 도엽번호)")
    ap.add_argument("--src-5k", help="1:5,000 도엽 폴더 (8자리 도엽번호, 폴백)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--radius", type=float, default=MATCH_RADIUS)
    args = ap.parse_args()

    from pyproj import Transformer
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    inv = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True).transform

    print("[1/3] 수치지형도 보도 적재")
    p1, a1 = _load_polys(args.src_1k, 9)
    print(f"  1:1,000 폴리곤 {len(p1)}")
    p5, a5 = ([], [])
    if args.src_5k:
        p5, a5 = _load_polys(args.src_5k, 8)
        print(f"  1:5,000 폴리곤 {len(p5)}")
    tiers = [("topo1k", STRtree(p1), p1, a1)] if p1 else []
    if p5:
        tiers.append(("topo5k", STRtree(p5), p5, a5))

    print("[2/3] 그래프 링크 매칭")
    with open(args.graph, "rb") as f:
        G = pickle.load(f)

    def _lonlat(n):
        a = G.nodes[n]
        if "lon" in a:
            return a["lon"], a["lat"]
        if "x" in a:
            return a["x"], a["y"]
        return None

    def sample_pts(u, v, d):
        """(lon, lat) 표본점. geometry 는 02 그래프에서 좌표쌍 list — 쌍 순서는
        값 크기로 판별한다 (안양: lat≈37, lon≈127)."""
        geom = d.get("geometry")
        pts = []
        if isinstance(geom, (list, tuple)) and len(geom) >= 2:
            for pair in (geom[len(geom) // 4], geom[len(geom) // 2], geom[(3 * len(geom)) // 4]):
                a, b = float(pair[0]), float(pair[1])
                lon, lat = (a, b) if a > 90 else (b, a)
                pts.append(Point(lon, lat))
            return pts
        if geom is not None and hasattr(geom, "interpolate"):
            return [geom.interpolate(t, normalized=True) for t in (0.25, 0.5, 0.75)]
        pu, pv = _lonlat(u), _lonlat(v)
        if pu and pv:
            return [Point((pu[0] + pv[0]) / 2, (pu[1] + pv[1]) / 2)]
        return []

    # road(이면도로)는 차도 보행이라 옆 보도의 폭을 옮기면 의미가 틀어진다 — 제외.
    ENRICH_TYPES = {"sidewalk", "crossing", "steps", "overpass", "underpass",
                    "ramp", "unknown"}

    stat = {"edges": 0, "had_width": 0, "filled_width": 0, "filled_surface": 0,
            "by_tier": {}}
    for u, v, d in G.edges(data=True):
        stat["edges"] += 1
        if d.get("link_type") not in ENRICH_TYPES:
            continue
        if d.get("width"):
            stat["had_width"] += 1
        pts = [Point(*inv(p.x, p.y)) for p in sample_pts(u, v, d)]
        if not pts:
            continue
        got = None
        for tier, tree, polys, attrs in tiers:
            best, bd = None, 1e18
            for p in pts:
                for i in tree.query(p.buffer(args.radius)):
                    dd = polys[i].distance(p)
                    if dd < bd:
                        best, bd = i, dd
            if best is not None and bd <= args.radius:
                got = (tier, attrs[best])
                break
        if got is None:
            continue
        tier, a = got
        stat["by_tier"][tier] = stat["by_tier"].get(tier, 0) + 1
        if a["width"] and not d.get("width"):
            d["width"] = a["width"]
            stat["filled_width"] += 1
        if a["surface"] and not d.get("surface"):
            d["surface"] = a["surface"]
            stat["filled_surface"] += 1
        d["topo_source"] = tier

    print("[3/3] 저장")
    with open(args.out, "wb") as f:
        pickle.dump(G, f)
    print(f"  링크 {stat['edges']} / 기존 폭 보유 {stat['had_width']}"
          f" / 폭 신규 채움 {stat['filled_width']} / 재질 채움 {stat['filled_surface']}")
    print(f"  매칭 계층: {stat['by_tier']}")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
