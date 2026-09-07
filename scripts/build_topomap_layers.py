# -*- coding: utf-8 -*-
"""수치지형도(1:1000) 도엽에서 정제·스냅에 쓰는 면형 레이어를 한 번 뽑아 둔다 (#67, v1.23.0).

산출 (WGS84 GeoJSON):
  <out>/sidewalk_polys.geojson   인도 면형(A0033320) — 폭·도엽 id 속성. 직결 링크 정제(H1·인셋·파편화)에 쓴다
  <out>/obstacles.geojson        계단 면형·옹벽·담장·가드펜스·수목 (topomap.obstacles.ObstacleIndex 직렬화)

    python scripts/build_topomap_layers.py --src "<도엽 폴더>" --out data/topomap_layers
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.topomap.extract import extract_sheet  # noqa: E402
from route_service.topomap.obstacles import build_index  # noqa: E402

SRC_EPSG = 5186


def _sheets(src: str) -> list[str]:
    out = sorted(glob.glob(os.path.join(src, "**", "*.zip"), recursive=True))
    seen, uniq = set(), []
    for p in out:
        m = re.search(r"_(\d{9})_", os.path.basename(p))
        k = m.group(1) if m else p
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", default="data/topomap_layers")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    from pyproj import Transformer
    from shapely.geometry import Polygon, mapping
    from shapely.ops import transform
    fwd = Transformer.from_crs(f"EPSG:{SRC_EPSG}", "EPSG:4326", always_xy=True).transform

    sheets = _sheets(a.src)
    if a.limit:
        sheets = sheets[:a.limit]
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    feats = []
    for i, p in enumerate(sheets):
        for f in extract_sheet(p):
            if f.get("kind") != "sidewalk_poly":
                continue
            pts, parts = f["points"], list(f["parts"]) + [len(f["points"])]
            rings = [pts[parts[j]:parts[j + 1]] for j in range(len(parts) - 1)]
            rings = [r for r in rings if len(r) >= 4]
            if not rings:
                continue
            try:
                poly = Polygon(rings[0], rings[1:])
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_empty:
                    continue
                g = transform(lambda x, y, z=None: fwd(x, y), poly)
            except Exception:
                continue
            attrs = f.get("attrs") or {}
            w = attrs.get("폭") or attrs.get("WIDTH")
            try:
                w = float(str(w).strip()) if w not in (None, "") else None
            except ValueError:
                w = None
            feats.append({"type": "Feature", "properties": {"sheet": f["sheet"], "width": w},
                          "geometry": mapping(g)})
        if (i + 1) % 20 == 0:
            print("  sidewalk %d/%d sheets, %d polys, %.0fs" % (i + 1, len(sheets), len(feats), time.time() - t0), flush=True)
    with open(os.path.join(a.out, "sidewalk_polys.geojson"), "w", encoding="utf-8") as fp:
        json.dump({"type": "FeatureCollection", "features": feats}, fp)
    print("sidewalk_polys: %d (%.0fs)" % (len(feats), time.time() - t0), flush=True)

    idx = build_index(sheets, to_wgs84=fwd)
    n = idx.to_geojson(os.path.join(a.out, "obstacles.geojson"))
    print("obstacles: %d %s (%.0fs)" % (n, idx.counts(), time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
