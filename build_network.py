# build_network.py
# -*- coding: utf-8 -*-
import os
import numpy as np
import pandas as pd
import xdem
import networkx as nx
from shapely.geometry import LineString
from pyproj import Transformer
from tqdm import tqdm
import pickle

# -----------------------------
# 0. 경로 설정
# -----------------------------
base_dir = r"D:\workspace\SW공인시험\Pathfinding\tests"

data_dir = r"D:\workspace\SW공인시험\Pathfinding\data\geomap\인천_도보_네트워크\data"
dem_path = r"D:\workspace\SW공인시험\Pathfinding\data\dem\90m\DEM_인천_37611_2022.img"

graph_path = os.path.join(base_dir, "network.gpickle")

# -----------------------------
# 1. 데이터 로드 & DEM
# -----------------------------
node = pd.read_excel(os.path.join(data_dir, "node.xlsx"))
link = pd.read_excel(os.path.join(data_dir, "link.xlsx"))

dem = xdem.DEM(dem_path)
to_dem_crs = Transformer.from_crs("EPSG:4326", dem.crs, always_xy=True)

# DEM crop (기존 사용 영역과 동일)
lat_min_dem, lat_max_dem = 37.4560 - 0.005, 37.4690 + 0.005
lon_min_dem, lon_max_dem = 126.6946 - 0.005, 126.7112 + 0.005
x_min, y_min = to_dem_crs.transform(lon_min_dem, lat_min_dem)
x_max, y_max = to_dem_crs.transform(lon_max_dem, lat_max_dem)
bbox = [x_min, y_min, x_max, y_max]
clipped_dem = dem.crop(bbox)

slope_raster = xdem.terrain.slope(clipped_dem)
slope_array = slope_raster.data.filled(np.nan)
transform = clipped_dem.transform

# -----------------------------
# 2. 그래프 생성
# -----------------------------
def create_graph(graph_type, node_df, link_df):
    G = graph_type()
    for _, row in node_df.iterrows():
        G.add_node(row["node_id"], lat=row["latitude"], lon=row["longitude"])
    for _, row in link_df.iterrows():
        if row["s_node_id"] in G.nodes and row["e_node_id"] in G.nodes:
            x0, y0 = to_dem_crs.transform(
                G.nodes[row["s_node_id"]]["lon"], G.nodes[row["s_node_id"]]["lat"]
            )
            x1, y1 = to_dem_crs.transform(
                G.nodes[row["e_node_id"]]["lon"], G.nodes[row["e_node_id"]]["lat"]
            )
            G.add_edge(
                row["s_node_id"],
                row["e_node_id"],
                length=float(row["length"]),
                link_name=row["link_name"],
                geometry=LineString([(x0, y0), (x1, y1)]),
            )
    return G

G = create_graph(nx.Graph, node, link)

# -----------------------------
# 3. 경사 계산 (weight는 계산만 해둠, 지금은 length만 사용)
# -----------------------------
def get_slope_at(x, y, transform, array):
    col, row = ~transform * (x, y)
    try:
        return array[int(row), int(col)]
    except IndexError:
        return np.nan

print("[build_network] Extracting slope from DEM and computing edge attributes...")
for u, v, data in tqdm(G.edges(data=True)):
    geom = data["geometry"]
    num_points = max(int(geom.length // 5), 1)
    points = [
        geom.interpolate(i / num_points, normalized=True)
        for i in range(num_points + 1)
    ]
    slopes = [get_slope_at(pt.x, pt.y, transform, slope_array) for pt in points]
    valid_slopes = [s for s in slopes if not np.isnan(s)]
    mean_slope = np.mean(valid_slopes) if valid_slopes else 0.0

    data["slope"] = float(mean_slope)
    length = float(data.get("length", 1.0))
    # 나중에 쓸 수도 있으니 weight는 일단 저장해 둠 (현재 shortest path에는 사용 X)
    data["weight"] = length * (1.0 + 0.1 * mean_slope)

# -----------------------------
# 4. 그래프 저장
# -----------------------------
   
with open(graph_path, "wb") as f:
    pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)

print(f"[build_network] Saved network to: {graph_path}")
