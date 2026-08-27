# -*- coding: utf-8 -*-
"""안양시 횡단보도(mv_crosswalk)에 접속 보도 유효폭을 채운다.

원천 CSV 의 width_m 은 도색 띠 폭(횡단보도 자체 폭)이고, 법정 판정에 필요한 것은
횡단보도에 **접속하는 보도의 유효폭**이다. 수치지형도 1:1,000 보도 중심선
(pednet_link_cw.csv, WIDTH 기재율 73.5%)에서 반경 내 최근접 폭 기재 보도 링크의
폭을 전이한다. 커버리지 실측 95.3% (2,600/2,728, 반경 25m).

출처는 approach_width_src='topo1k' 로 표기한다 — 현장 실측값이 아니라
수치지형도 폭원 속성의 전이값임을 소비자가 알 수 있어야 한다.

사용:
  python scripts/fill_crosswalk_widths.py \
      --crosswalks data/crosswalks_anyang_city.geojson \
      --pednet-node data/pednet_node_cw.csv \
      --pednet-link data/pednet_link_cw.csv \
      --out data/db_export/mv_crosswalk_approach_width.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os

RADIUS = 25.0        # m — 접속 보도 탐색 반경 (커버리지 실측과 동일 기준)
DENSIFY = 5.0        # m — 링크 샘플점 간격 (KDTree 후보 추출용)


def _load_crosswalks(path, to_5186):
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        g = feat.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        p = feat.get("properties") or {}
        if p.get("src") and p["src"] != "anyang_city_2026":
            continue
        lon, lat = g["coordinates"][0], g["coordinates"][1]
        out.append({"mgmt_no": p.get("mgmt_no"),
                    "src_version": p.get("src_version") or "20260826",
                    "xy": to_5186(lon, lat)})
    return out


def _load_sidewalk_segments(node_csv, link_csv, to_5186):
    """폭이 기재된 보도 링크 -> 5186 선분 목록 [(x1,y1,x2,y2,width)]."""
    nodes = {}
    with open(node_csv, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            nodes[r["NODE_ID"]] = to_5186(float(r["X"]), float(r["Y"]))
    segs = []
    with open(link_csv, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("LINK_TYPE") != "sidewalk":
                continue
            w = (r.get("WIDTH") or "").strip()
            if not w:
                continue
            try:
                w = float(w)
            except ValueError:
                continue
            if w <= 0:
                continue
            a = nodes.get(r["F_NODE"])
            b = nodes.get(r["T_NODE"])
            if a is None or b is None:
                continue
            segs.append((a[0], a[1], b[0], b[1], w))
    return segs


def _pt_seg_dist(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crosswalks", required=True)
    ap.add_argument("--pednet-node", required=True)
    ap.add_argument("--pednet-link", required=True)
    ap.add_argument("--out", default="data/db_export/mv_crosswalk_approach_width.csv")
    ap.add_argument("--radius", type=float, default=RADIUS)
    args = ap.parse_args()

    import numpy as np
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    to_5186 = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True).transform

    cw = _load_crosswalks(args.crosswalks, to_5186)
    segs = _load_sidewalk_segments(args.pednet_node, args.pednet_link, to_5186)
    print(f"횡단보도 {len(cw)} / 폭 기재 보도 링크 {len(segs)}")

    # 링크를 DENSIFY 간격 샘플점으로 펼쳐 KDTree 후보 추출 -> 정확 거리는 선분 투영으로
    pts, owner = [], []
    for i, (x1, y1, x2, y2, _w) in enumerate(segs):
        n = max(2, int(math.hypot(x2 - x1, y2 - y1) / DENSIFY) + 1)
        for t in np.linspace(0.0, 1.0, n):
            pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
            owner.append(i)
    tree = cKDTree(np.array(pts))
    owner = np.array(owner)

    rows, dists, widths = [], [], []
    for c in cw:
        px, py = c["xy"]
        idxs = tree.query_ball_point((px, py), r=args.radius + DENSIFY)
        best = None  # (dist, width)
        for si in set(owner[idxs].tolist()) if idxs else ():
            x1, y1, x2, y2, w = segs[si]
            d = _pt_seg_dist(px, py, x1, y1, x2, y2)
            if d <= args.radius and (best is None or d < best[0]):
                best = (d, w)
        if best is not None:
            rows.append({"src_version": c["src_version"], "mgmt_no": c["mgmt_no"],
                         "approach_width_m": round(best[1], 2),
                         "approach_width_dist_m": round(best[0], 1),
                         "approach_width_src": "topo1k"})
            dists.append(best[0])
            widths.append(best[1])

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["src_version", "mgmt_no", "approach_width_m",
                                          "approach_width_dist_m", "approach_width_src"])
        w.writeheader()
        w.writerows(rows)

    widths_sorted = sorted(widths)

    def pct(p):
        return widths_sorted[min(len(widths_sorted) - 1,
                                 int(p / 100.0 * len(widths_sorted)))] if widths_sorted else None

    below = sum(1 for w_ in widths if w_ < 1.2)
    print(f"채움 {len(rows)}/{len(cw)} ({len(rows)/len(cw)*100:.1f}%)  "
          f"p10/p50/p90 = {pct(10)}/{pct(50)}/{pct(90)} m  "
          f"법정 1.2m 미만 {below}건  거리 중앙값 {sorted(dists)[len(dists)//2]:.1f}m")
    print(f"산출: {args.out}")


if __name__ == "__main__":
    main()
