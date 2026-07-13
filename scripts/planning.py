# -*- coding: utf-8 -*-
"""배치 경로 탐색 (기존 planning.py 호환).

queries.json 의 질의를 읽어 경로를 산출하고 preds.jsonl 로 저장한다.
질의는 노드 ID 쌍({"start","goal"}) 또는 좌표({"origin":{lat,lng},"destination":{lat,lng}})
둘 다 지원한다. 프로필을 지정하면 경사·계단 회피가 적용된다.

사용 예)
  python scripts/planning.py --network data/network_anyang.gpickle \
      --queries queries.json --profile wheelchair_manual --out results/preds.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_service.engine.graph import NetworkStore  # noqa: E402
from route_service.engine.planner import NoRouteError, plan  # noqa: E402
from route_service.engine.profiles import get_profile  # noqa: E402
from route_service.engine.snap import snap  # noqa: E402
from route_service.engine.steps import build_steps  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="배치 경로 탐색")
    ap.add_argument("--network", default="data/network.gpickle")
    ap.add_argument("--queries", default="queries.json")
    ap.add_argument("--profile", default="wheelchair_manual")
    ap.add_argument("--out", default="results/preds.jsonl")
    ap.add_argument("--steps", action="store_true", help="턴바이턴 안내도 함께 저장")
    args = ap.parse_args()

    store = NetworkStore()
    meta = store.load(args.network)
    print("[planning] 네트워크: nodes=%s edges=%s" % (meta["node_cnt"], meta["edge_cnt"]))

    profile = get_profile(args.profile)
    with open(args.queries, encoding="utf-8") as f:
        queries = json.load(f)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    ok = 0
    with open(args.out, "w", encoding="utf-8") as fo:
        for idx, q in enumerate(queries):
            if "start" in q and "goal" in q:
                start, goal = q["start"], q["goal"]
            else:
                o, d = q["origin"], q["destination"]
                start = snap(store, o["lat"], o["lng"], profile)["node_id"]
                goal = snap(store, d["lat"], d["lng"], profile)["node_id"]

            rec = {"id": "map_0_%d" % idx, "start": start, "goal": goal, "profile": profile.id}
            try:
                res = plan(store, start, goal, profile, alternatives=1)
                r = res["routes"][0]
                rec.update(
                    {
                        "path": r["path"],
                        "geometry": r["geometry"],
                        "summary": r["summary"],
                        "fallback": res["fallback"],
                    }
                )
                if args.steps:
                    rec["steps"] = build_steps(store.graph, r["path"], profile)
                ok += 1
            except NoRouteError as e:
                rec.update({"path": [], "error": str(e)})
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print("[planning] 저장: %s (성공 %d/%d)" % (args.out, ok, len(queries)))


if __name__ == "__main__":
    main()
