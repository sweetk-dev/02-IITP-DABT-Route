# planning.py
# -*- coding: utf-8 -*-
import os
import json
import numpy as np
import networkx as nx
from math import sqrt
import pickle
import folium  # ✅ Folium

# -----------------------------
# 0. 경로 설정
# -----------------------------
base_dir = r"D:\workspace\SW공인시험\Pathfinding\tests"

graph_path   = os.path.join(base_dir, "network.gpickle")
queries_path = os.path.join(base_dir, "queries.json")

results_dir  = os.path.join(base_dir, "results")
preds_path   = os.path.join(results_dir, "preds.jsonl")
preds_map    = os.path.join(results_dir, "preds_map.html")

os.makedirs(results_dir, exist_ok=True)

max_slope = 4.0  # slope < 4.0 엣지만 허용

# -----------------------------
# 1. 네트워크 로드
# -----------------------------
print(f"[planning] Loading network from: {graph_path}")
with open(graph_path, "rb") as f:
    G = pickle.load(f)
print(f"[planning] Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

# -----------------------------
# 2. slope < 4° 엣지만 쓰는 그래프
# -----------------------------
def build_constrained_graph(G, slope_threshold):
    G_sub = nx.Graph()
    G_sub.add_nodes_from(G.nodes(data=True))
    for u, v, data in G.edges(data=True):
        if data.get("slope", 0.0) < slope_threshold:
            G_sub.add_edge(u, v, **data)
    return G_sub

G_constrained = build_constrained_graph(G, max_slope)
print(
    f"[planning] Constrained graph (slope < {max_slope}°): "
    f"{G_constrained.number_of_nodes()} nodes, {G_constrained.number_of_edges()} edges"
)

# -----------------------------
# 3. A* 휴리스틱 & 요약
# -----------------------------
def heuristic(u, v):
    dx = G.nodes[u]["lon"] - G.nodes[v]["lon"]
    dy = G.nodes[u]["lat"] - G.nodes[v]["lat"]
    return sqrt(dx**2 + dy**2)

def summarize_path(G_ref, path):
    if not path or len(path) < 2:
        return 0.0, 0.0, 0.0
    lengths = [G_ref[u][v]["length"] for u, v in zip(path[:-1], path[1:])]
    slopes  = [G_ref[u][v]["slope"]  for u, v in zip(path[:-1], path[1:])]
    total_length = float(sum(lengths))
    mean_slope   = float(np.mean(slopes)) if slopes else 0.0
    max_slope_val = float(np.max(slopes)) if slopes else 0.0
    return total_length, mean_slope, max_slope_val

# -----------------------------
# 4. queries.json 돌면서 예측 경로 생성
# -----------------------------
with open(queries_path, "r", encoding="utf-8") as f:
    queries = json.load(f)

preds = []

for idx, q in enumerate(queries):
    start = q["start"]
    goal  = q["goal"]
    result_id = f"map_0_{idx}"

    try:
        # 라벨과 동일 조건:
        #   - slope < 4° 그래프
        #   - weight="length"
        path = nx.astar_path(
            G_constrained,
            source=start,
            target=goal,
            weight="length",
            heuristic=heuristic,
        )
        total_length, mean_slope, max_slope_val = summarize_path(G, path)
    except nx.NetworkXNoPath:
        path = []
        total_length, mean_slope, max_slope_val = 0.0, 0.0, 0.0

    preds.append({
        "id": result_id,
        "start": start,
        "goal": goal,
        "path": path,
        "total_length": total_length,
        "mean_slope": mean_slope,
        "max_slope": max_slope_val,
    })

# -----------------------------
# 5. JSONL 저장
# -----------------------------
with open(preds_path, "w", encoding="utf-8") as f:
    for rec in preds:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

print(f"[planning] Saved predictions to: {preds_path}")

# -----------------------------
# 6. Route별 Folium 지도 저장
# -----------------------------
import folium  # 파일 상단에 없다면 추가

for idx, rec in enumerate(preds):
    path = rec["path"]
    if not path:
        continue  # 경로 없으면 스킵

    coords = []
    for nid in path:
        if nid in G.nodes:
            coords.append((G.nodes[nid]["lat"], G.nodes[nid]["lon"]))

    if len(coords) < 2:
        continue

    lats = [lat for lat, lon in coords]
    lons = [lon for lat, lon in coords]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    m = folium.Map(location=[center_lat, center_lon], zoom_start=16)

    # 예측 경로 - 빨간색
    folium.PolyLine(
        locations=coords,
        color="red",
        weight=5,
        opacity=0.8,
        tooltip=f"Pred {rec['id']} (len={len(coords)})"
    ).add_to(m)

    s_lat, s_lon = coords[0]
    g_lat, g_lon = coords[-1]

    folium.Marker(
        location=(s_lat, s_lon),
        icon=folium.Icon(color="green", icon="play", prefix="fa"),
        tooltip=f"Start: {rec['start']}"
    ).add_to(m)

    folium.Marker(
        location=(g_lat, g_lon),
        icon=folium.Icon(color="red", icon="stop", prefix="fa"),
        tooltip=f"Goal: {rec['goal']}"
    ).add_to(m)

    # ✅ 파일 이름: run_R{k}_pred.html
    route_idx = idx + 1
    map_path = os.path.join(results_dir, f"run_R{route_idx}_pred.html")
    m.save(map_path)
    print(f"[planning] Saved route {route_idx} map to: {map_path}")
