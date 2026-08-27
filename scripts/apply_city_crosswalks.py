# -*- coding: utf-8 -*-
"""안양시 횡단보도 원천(mv_crosswalk)을 OSM 보행 그래프에 반영한다.

3단계로 나눠 반영한다. 안양 OSM 보행망은 도로 중심선 위주(road 6,917 / sidewalk 2,063)라
횡단보도 점 대부분이 "건너편 보도선"을 못 찾는다(2,728건 중 양쪽 뱅크 성립 189건).
그래서 링크 생성을 강행하지 않고, 붙일 수 있는 만큼만 링크로 붙이고 나머지는 지점으로 부착한다.

  [1] 매칭  — 기존 crossing 링크에 원천 속성 전이 (폭·길이·턱낮춤·점자블록)
  [2] 신설  — 양쪽에 별개의 보행 링크가 실재하는 경우만 신규 crossing 링크 생성
  [3] 부착  — 나머지는 최근접 보행 노드에 지점 메타로 부착 (안내 문구·경고용)

curb_cut / tactile_paving 의 None 은 "없음"이 아니라 **미상**이다.
planner.edge_passable 은 curb_cut is False 일 때만 차단하므로 이 계약을 그대로 유지한다.
원천 기재율이 4.4% 라 미상을 없음으로 보수 처리하면 만안구 경로가 전멸한다.

사용:
  python scripts/apply_city_crosswalks.py \
      --graph data/network_anyang_enriched.gpickle \
      --crosswalks data/crosswalks_anyang_city.geojson \
      --out data/network_anyang_cw.gpickle \
      --report data/db_export/crosswalk_apply_report.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MATCH_RADIUS = 30.0    # m - 기존 crossing 링크 중점에서 원천 점을 찾는 반경
BANK_ALONG   = 10.0    # m - 횡단 대상 도로축 방향 허용 오차
BANK_MIN     = 1.5     # m - 이보다 가까우면 같은 지점으로 본다
SPAN_MAX     = 60.0    # m - 이보다 긴 횡단 링크는 오접합으로 보고 버린다
NODE_SNAP    = 3.0     # m - 투영점이 이 안이면 기존 노드를 그대로 쓴다
ATTACH_RADIUS = 30.0   # m - 지점 부착 반경
SRC = "city_cw2026"


def _load_crosswalks(path, to_5186):
    import numpy as np
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)
    recs = []
    for feat in gj.get("features", []):
        g = feat.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        p = feat.get("properties") or {}
        if p.get("src") and p["src"] != "anyang_city_2026":
            continue
        lon, lat = g["coordinates"][0], g["coordinates"][1]
        recs.append({
            "mgmt_no": p.get("mgmt_no"),
            "lon": lon, "lat": lat,
            "xy": to_5186(lon, lat),
            "width": p.get("cw_width_m"),
            "length": p.get("cw_length_m"),
            "curb_cut": p.get("curb_cut"),
            "tactile": p.get("tactile_paving"),
            "dummy": p.get("survey_flag") == "dummy",
        })
    return recs


def main():
    import numpy as np
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--crosswalks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report")
    args = ap.parse_args()

    to_5186 = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True).transform
    to_wgs  = Transformer.from_crs("EPSG:5186", "EPSG:4326", always_xy=True).transform

    with open(args.graph, "rb") as f:
        G = pickle.load(f)
    cw = _load_crosswalks(args.crosswalks, to_5186)
    print("[0/4] 그래프 노드 %d / 링크 %d, 원천 횡단보도 %d"
          % (G.number_of_nodes(), G.number_of_edges(), len(cw)))

    stat = {"total": len(cw), "matched": 0, "matched_links": 0, "created": 0,
            "attached": 0, "orphan": 0, "filled_width": 0, "filled_curb": 0,
            "filled_tactile": 0, "split_nodes": 0, "span_reject": 0, "dup_reject": 0}
    assign = {}

    XY = {n: to_5186(a["lon"], a["lat"]) for n, a in G.nodes(data=True)}
    P = np.array([r["xy"] for r in cw])
    used = set()

    # ---------------------------------------------------------------- [1] 매칭
    # 기존 crossing 링크마다 반경 안 최근접 원천 점의 속성을 전이한다.
    # OSM 은 중앙분리대가 있는 횡단보도를 두 링크로 쪼개 두는 경우가 있어
    # 한 원천 점이 여러 링크에 대응할 수 있다 - 1:1 로 묶지 않는다.
    ptree = cKDTree(P)
    for u, v, e in list(G.edges(data=True)):
        if e.get("link_type") != "crossing":
            continue
        mx = np.array(((XY[u][0] + XY[v][0]) / 2.0, (XY[u][1] + XY[v][1]) / 2.0))
        d, i = ptree.query(mx, k=1)
        if d > MATCH_RADIUS:
            continue
        i = int(i); r = cw[i]
        if r["width"]:
            e["width"] = float(r["width"]); stat["filled_width"] += 1
        if r["length"]:
            e["cw_length_m"] = float(r["length"])
        if r["curb_cut"] is not None:
            e["curb_cut"] = bool(r["curb_cut"]); stat["filled_curb"] += 1
        if r["tactile"] is not None:
            e["tactile_paving"] = bool(r["tactile"]); stat["filled_tactile"] += 1
        e["cw_mgmt_no"] = r["mgmt_no"]
        e["attr_source"] = SRC
        stat["matched_links"] += 1
        if i not in used:
            used.add(i)
            stat["matched"] += 1
            assign[r["mgmt_no"]] = {"how": "exist", "dist": round(float(d), 1)}
    print("[1/4] 기존 crossing 링크 %d건에 원천 속성 전이 (원천 %d건 소화, "
          "폭 %d / 턱낮춤 %d / 점자블록 %d)"
          % (stat["matched_links"], stat["matched"], stat["filled_width"],
             stat["filled_curb"], stat["filled_tactile"]))

    # ---------------------------------------------------------------- [2] 신설
    # 양쪽에 별개의 보행 링크가 실재할 때만 신규 crossing 링크를 만든다.
    # 도로 중심선 위 두 점을 잇는 자기루프를 막기 위해 "마주보기" 조건을 건다.
    #
    # 2패스로 나눈다. 1패스에서 원본 형상만 보고 접속점(edge, t)을 전부 계산하고,
    # 2패스에서 링크별로 모아 한 번에 분할한다. 한 패스로 하면 이미 분할한 링크를
    # 다시 분할하려다 접속점이 기존 노드로 붕괴해 링크가 대량으로 사라진다.
    E = [(u, v, d) for u, v, d in G.edges(data=True)]
    A = np.array([XY[u] for u, v, d in E])
    B = np.array([XY[v] for u, v, d in E])
    TY = [d.get("link_type") for u, v, d in E]
    AB = B - A
    L2 = (AB ** 2).sum(1); L2[L2 == 0] = 1e-9
    mid = (A + B) / 2.0
    half = np.sqrt(L2) / 2.0
    mtree = cKDTree(mid)
    HM = float(half.max())

    def near_edges(p, rad):
        idx = np.array(mtree.query_ball_point(p, rad + HM), dtype=int)
        if idx.size == 0:
            return []
        t = np.clip(((p - A[idx]) * AB[idx]).sum(1) / L2[idx], 0, 1)
        proj = A[idx] + t[:, None] * AB[idx]
        dd = np.sqrt(((proj - p) ** 2).sum(1))
        keep = dd <= rad
        return list(zip(idx[keep], proj[keep], dd[keep], t[keep]))

    # --- 1패스: 접속점 수집
    plan = []          # (cw_index, (edge_k, t, proj), (edge_k, t, proj), span)
    cuts = {}          # edge_k -> [t, ...]
    for i, r in enumerate(cw):
        if i in used:
            continue
        p = P[i]
        L = float(r["length"] or 0) or 15.0
        rad = min(max(L * 0.75, 12.0), 40.0)
        cand = near_edges(p, rad)
        if not cand:
            continue
        roads = [c for c in cand if TY[int(c[0])] == "road"]
        ref = min(roads, key=lambda c: c[2]) if roads else min(cand, key=lambda c: c[2])
        j = int(ref[0])
        ax = AB[j] / math.sqrt(L2[j])
        nv = np.array([-ax[1], ax[0]])
        sides = {1: None, -1: None}
        for (k, proj, dd, t) in cand:
            if int(k) == j:
                continue
            rel = proj - p
            perp = float(rel @ nv)
            if abs(float(rel @ ax)) > BANK_ALONG or abs(perp) < BANK_MIN:
                continue
            s = 1 if perp > 0 else -1
            if sides[s] is None or abs(perp) < abs(sides[s][1]):
                sides[s] = (int(k), float(t), proj, abs(perp))
        if not (sides[1] and sides[-1]):
            continue
        span = float(np.linalg.norm(sides[1][2] - sides[-1][2]))
        if span > min(max(L * 1.6, 20.0), SPAN_MAX):
            stat["span_reject"] += 1
            continue
        plan.append((i, sides[1], sides[-1], span))
        for s in (sides[1], sides[-1]):
            cuts.setdefault(s[0], []).append(s[1])

    # --- 2패스: 링크별 일괄 분할
    node_at = {}       # (edge_k, t) -> node_id
    new_id = 0
    for k, ts in cuts.items():
        u, v, _ = E[k]
        if not G.has_edge(u, v):
            continue
        old = dict(G[u][v])
        seg_len = float(old.get("length") or math.sqrt(L2[k]))
        pts = sorted(set(round(t, 6) for t in ts))
        chain = [(0.0, u)]
        for t in pts:
            pos = A[k] + t * AB[k]
            if float(np.linalg.norm(pos - XY[u])) <= NODE_SNAP:
                node_at[(k, t)] = u; continue
            if float(np.linalg.norm(pos - XY[v])) <= NODE_SNAP:
                node_at[(k, t)] = v; continue
            new_id += 1
            nid = "cwx%06d" % new_id
            lon, lat = to_wgs(float(pos[0]), float(pos[1]))
            G.add_node(nid, lat=lat, lon=lon, node_type="crossing")
            XY[nid] = (float(pos[0]), float(pos[1]))
            node_at[(k, t)] = nid
            chain.append((t, nid))
            stat["split_nodes"] += 1
        if len(chain) == 1:
            continue
        chain.append((1.0, v))
        G.remove_edge(u, v)
        for (t0, n0), (t1, n1) in zip(chain[:-1], chain[1:]):
            if n0 == n1:
                continue
            a = dict(old)
            a["length"] = max(seg_len * (t1 - t0), 0.1)
            a["geometry"] = None
            G.add_edge(n0, n1, **a)

    # --- 횡단 링크 연결
    for i, s1, s2, span in plan:
        r = cw[i]
        na = node_at.get((s1[0], round(s1[1], 6)))
        nb = node_at.get((s2[0], round(s2[1], 6)))
        if na is None or nb is None or na == nb or G.has_edge(na, nb):
            stat["dup_reject"] += 1
            continue
        G.add_edge(na, nb, length=span, slope=0.0, link_type="crossing",
                   width=(float(r["width"]) if r["width"] else None),
                   curb_cut=(bool(r["curb_cut"]) if r["curb_cut"] is not None else None),
                   tactile_paving=(bool(r["tactile"]) if r["tactile"] is not None else None),
                   surface=None, link_name=None, geometry=None,
                   cw_mgmt_no=r["mgmt_no"], cw_length_m=r["length"], attr_source=SRC)
        used.add(i)
        stat["created"] += 1
        assign[r["mgmt_no"]] = {"how": "new", "node": str(na), "dist": round(span, 1)}
    print("[2/4] 신규 crossing 링크 %d건 생성 (후보 %d · 노드 분할 %d · "
          "스팬 초과 기각 %d · 중복 기각 %d)"
          % (stat["created"], len(plan), stat["split_nodes"],
             stat["span_reject"], stat["dup_reject"]))

    # ---------------------------------------------------------------- [3] 부착
    # 링크로 못 붙인 나머지는 최근접 보행 노드에 "지점"으로 단다.
    # 위상은 바뀌지 않는다. 경로가 이 노드를 지날 때 횡단 안내를 낼 수 있게 하는 것이 목적.
    ids = list(G.nodes)
    NXY = np.array([XY[n] for n in ids])
    ntree = cKDTree(NXY)
    for i, r in enumerate(cw):
        if i in used:
            continue
        d, k = ntree.query(P[i], k=1)
        if d > ATTACH_RADIUS:
            stat["orphan"] += 1
            assign[r["mgmt_no"]] = {"how": "orphan", "dist": round(float(d), 1)}
            continue
        n = ids[int(k)]
        a = G.nodes[n]
        a["crosswalk_cnt"] = int(a.get("crosswalk_cnt") or 0) + 1
        lst = a.get("cw_mgmt_nos") or []
        lst.append(r["mgmt_no"])
        a["cw_mgmt_nos"] = lst
        if r["curb_cut"] is not None:
            prev = a.get("cw_curb_cut")
            a["cw_curb_cut"] = bool(r["curb_cut"]) if prev is None else (prev and bool(r["curb_cut"]))
        if r["tactile"] is not None:
            prev = a.get("cw_tactile_paving")
            a["cw_tactile_paving"] = bool(r["tactile"]) if prev is None else (prev and bool(r["tactile"]))
        if a.get("node_type") in (None, "unknown"):
            a["node_type"] = "crossing"
        stat["attached"] += 1
        assign[r["mgmt_no"]] = {"how": "point", "node": str(n), "dist": round(float(d), 1)}
    print("[3/4] 지점 부착 %d건 / 반경 밖 %d건" % (stat["attached"], stat["orphan"]))

    # ---------------------------------------------------------------- 저장
    with open(args.out, "wb") as f:
        pickle.dump(G, f)
    types = {}
    for _, _, d in G.edges(data=True):
        t = d.get("link_type")
        types[t] = types.get(t, 0) + 1
    stat["nodes"] = G.number_of_nodes()
    stat["edges"] = G.number_of_edges()
    stat["link_types"] = types
    stat["crossing_nodes"] = sum(1 for _, a in G.nodes(data=True) if a.get("crosswalk_cnt"))
    print("[4/4] 저장: %s  노드 %d / 링크 %d" % (args.out, stat["nodes"], stat["edges"]))
    print("      링크타입: %s" % types)
    print("      횡단보도가 달린 노드 %d개" % stat["crossing_nodes"])
    cover = stat["matched"] + stat["created"] + stat["attached"]
    print("      원천 반영률 %d/%d (%.1f%%)  [매칭 %d · 신설 %d · 부착 %d · 미반영 %d]"
          % (cover, stat["total"], cover / stat["total"] * 100,
             stat["matched"], stat["created"], stat["attached"],
             stat["total"] - cover))
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"stat": stat, "assign": assign}, f, ensure_ascii=False)
        print("      리포트: %s" % args.report)


if __name__ == "__main__":
    main()
