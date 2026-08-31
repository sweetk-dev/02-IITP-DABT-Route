# -*- coding: utf-8 -*-
"""실증 도보 구간을 그래프 두 벌로 각각 탐색해 전/후를 비교한다.

횡단보도 반영이 실제 안내에 무엇을 바꾸는지 링크 단위로 본다. 관측 항목:
  - 거리·소요시간·최대경사              : 경로 자체가 바뀌었는가
  - crossing 링크 수                    : 횡단 안내를 낼 수 있는가 (기존 지표)
  - 경로가 지나는 노드의 부착 횡단보도  : 링크로는 못 붙었지만 지점으로 아는 횡단보도
  - curb_cut 판정 분포(있음/없음/미상)  : 휠체어 통행 가부를 말할 수 있는 비율

사용:
  python scripts/simulate_crosswalk_effect.py \
      --before data/network_anyang_enriched.gpickle \
      --after  data/network_anyang_cw.gpickle \
      --legs   data/db_export/demo_legs.json \
      --out    data/db_export/crosswalk_sim_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import networkx as nx  # noqa: E402

from route_service.engine.geo import haversine_m          # noqa: E402
from route_service.engine.planner import edge_passable, edge_cost  # noqa: E402
from route_service.engine.profiles import get_profile     # noqa: E402


def _snap(G, lat, lon):
    best, bd = None, 1e18
    for n, a in G.nodes(data=True):
        d = haversine_m(lat, lon, a["lat"], a["lon"])
        if d < bd:
            best, bd = n, d
    return best, bd


def _route(G, prof, s, t):
    max_slope = prof.max_slope_deg

    def weight(u, v, data):
        if not edge_passable(data, prof, max_slope):
            return None
        return edge_cost(data, prof)

    def h(u, v):
        return haversine_m(G.nodes[u]["lat"], G.nodes[u]["lon"],
                           G.nodes[v]["lat"], G.nodes[v]["lon"])
    return nx.astar_path(G, s, t, heuristic=h, weight=weight)


def _measure(G, path, prof):
    dist = 0.0
    crossing = 0
    slopes = []
    curb = {"yes": 0, "no": 0, "unknown": 0}
    width_known = 0
    for u, v in zip(path[:-1], path[1:]):
        d = G[u][v]
        dist += float(d.get("length") or 0)
        slopes.append(float(d.get("slope") or 0))
        if d.get("link_type") == "crossing":
            crossing += 1
            c = d.get("curb_cut")
            curb["yes" if c is True else "no" if c is False else "unknown"] += 1
            if d.get("width"):
                width_known += 1
    node_cw = sum(int(G.nodes[n].get("crosswalk_cnt") or 0) for n in path)
    node_cw_nodes = sum(1 for n in path if G.nodes[n].get("crosswalk_cnt"))
    return {
        "distance_m": round(dist),
        "duration_s": round(dist / prof.speed_mps) if prof.speed_mps else None,
        "max_slope_deg": round(max(slopes), 2) if slopes else 0.0,
        "links": len(path) - 1,
        "crossing_links": crossing,
        "crossing_width_known": width_known,
        "curb_cut": curb,
        "node_crosswalks": node_cw,
        "node_crosswalk_nodes": node_cw_nodes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--legs", required=True)
    ap.add_argument("--profile", default="wheelchair_manual")
    ap.add_argument("--out")
    args = ap.parse_args()

    prof = get_profile(args.profile)
    legs = json.load(open(args.legs, encoding="utf-8"))
    graphs = {}
    for tag, p in (("before", args.before), ("after", args.after)):
        with open(p, "rb") as f:
            graphs[tag] = pickle.load(f)
        g = graphs[tag]
        print("%-6s 노드 %5d / 링크 %5d" % (tag, g.number_of_nodes(), g.number_of_edges()))

    rows = []
    for leg in legs:
        row = {"name": leg["name"]}
        for tag, G in graphs.items():
            s, ds = _snap(G, leg["from"][0], leg["from"][1])
            t, dt = _snap(G, leg["to"][0], leg["to"][1])
            try:
                path = _route(G, prof, s, t)
                m = _measure(G, path, prof)
            except Exception as e:
                m = {"error": str(e)}
            m["snap_m"] = [round(ds), round(dt)]
            row[tag] = m
        rows.append(row)

    print("\n%-26s %9s %9s %8s %8s %9s"
          % ("구간", "거리(전)", "거리(후)", "횡단(전)", "횡단(후)", "지점부착"))
    print("-" * 76)
    for r in rows:
        b, a = r.get("before", {}), r.get("after", {})
        print("%-26s %9s %9s %8s %8s %9s"
              % (r["name"][:26],
                 b.get("distance_m", "-"), a.get("distance_m", "-"),
                 b.get("crossing_links", "-"), a.get("crossing_links", "-"),
                 a.get("node_crosswalks", "-")))

    tot = {"crossing_before": 0, "crossing_after": 0, "node_cw": 0,
           "curb_known": 0, "curb_unknown": 0, "changed_route": 0}
    for r in rows:
        b, a = r.get("before", {}), r.get("after", {})
        tot["crossing_before"] += b.get("crossing_links", 0)
        tot["crossing_after"] += a.get("crossing_links", 0)
        tot["node_cw"] += a.get("node_crosswalks", 0)
        c = a.get("curb_cut") or {}
        tot["curb_known"] += c.get("yes", 0) + c.get("no", 0)
        tot["curb_unknown"] += c.get("unknown", 0)
        if b.get("distance_m") != a.get("distance_m"):
            tot["changed_route"] += 1
    print("\n합계  crossing 링크 %d -> %d / 노드 부착 횡단보도 %d / "
          "턱낮춤 판정가능 %d·미상 %d / 경로 변경 %d개 구간"
          % (tot["crossing_before"], tot["crossing_after"], tot["node_cw"],
             tot["curb_known"], tot["curb_unknown"], tot["changed_route"]))

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        json.dump({"profile": args.profile, "legs": rows, "total": tot},
                  open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("리포트: %s" % args.out)


if __name__ == "__main__":
    main()
