# -*- coding: utf-8 -*-
"""지형 타일 기반 링크 종단경사 산출.

국토정보플랫폼 5m DEM(.img)이 확보되기 전까지 쓰는 경로다(dem.py 는 그대로 유지).
공개 지형 타일(Terrarium PNG, 인증 불필요)에서 표고를 읽어 **링크의 종단경사**를 계산한다.

DEM 래스터의 '지형 경사도'가 아니라 링크를 따라가는 실제 오르내림(grade)을 쓰는 이유:
휠체어 통행 가부를 좌우하는 것은 주변 지형의 급함이 아니라 그 길을 실제로 얼마나
올라가야 하는가이기 때문이다. 경사도 래스터는 옆 절벽 때문에 평지 보도를 급경사로
오판할 수 있다.

표고 디코딩: elevation(m) = (R * 256 + G + B / 256) - 32768
"""
from __future__ import annotations

import math
import os

TILE_URL = os.environ.get(
    "TERRAIN_TILE_URL",
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
)
TILE_SIZE = 256


def _deg2tile(lat: float, lon: float, z: int):
    """위경도 -> 타일 좌표(소수점 포함)."""
    lat_r = math.radians(lat)
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


class TerrainSampler:
    """타일을 한 번만 받아 캐시하고, 위경도로 표고를 조회한다."""

    def __init__(self, zoom: int = 13, cache_dir: str = "data/terrain"):
        self.zoom = zoom
        self.cache_dir = cache_dir
        self._tiles = {}
        os.makedirs(cache_dir, exist_ok=True)

    def _tile_array(self, tx: int, ty: int):
        key = (tx, ty)
        if key in self._tiles:
            return self._tiles[key]

        import numpy as np
        from PIL import Image

        path = os.path.join(self.cache_dir, "%d_%d_%d.png" % (self.zoom, tx, ty))
        if not os.path.exists(path):
            import urllib.request

            url = TILE_URL.format(z=self.zoom, x=tx, y=ty)
            req = urllib.request.Request(url, headers={"User-Agent": "iitp-dabt-route/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
                f.write(r.read())

        img = Image.open(path).convert("RGB")
        a = np.asarray(img, dtype="float64")
        elev = (a[:, :, 0] * 256.0 + a[:, :, 1] + a[:, :, 2] / 256.0) - 32768.0
        self._tiles[key] = elev
        return elev

    def elevation(self, lat: float, lon: float) -> float:
        x, y = _deg2tile(lat, lon, self.zoom)
        tx, ty = int(math.floor(x)), int(math.floor(y))
        arr = self._tile_array(tx, ty)
        px = min(TILE_SIZE - 1, max(0, int((x - tx) * TILE_SIZE)))
        py = min(TILE_SIZE - 1, max(0, int((y - ty) * TILE_SIZE)))
        return float(arr[py, px])


def apply_slope_from_terrain(G, zoom: int = 13, cache_dir: str = "data/terrain") -> dict:
    """그래프의 각 링크에 종단경사(도)를 채운다. 반환: 통계."""
    from .geo import haversine_m

    sampler = TerrainSampler(zoom=zoom, cache_dir=cache_dir)
    elev_cache = {}

    def elev_of(node):
        if node not in elev_cache:
            d = G.nodes[node]
            elev_cache[node] = sampler.elevation(d["lat"], d["lon"])
        return elev_cache[node]

    filled = 0
    steepest = 0.0
    for u, v, data in G.edges(data=True):
        try:
            e1, e2 = elev_of(u), elev_of(v)
        except Exception:
            continue
        length = float(data.get("length") or 0.0) or haversine_m(
            G.nodes[u]["lat"], G.nodes[u]["lon"], G.nodes[v]["lat"], G.nodes[v]["lon"]
        )
        if length < 1.0:
            continue
        grade = abs(e2 - e1) / length            # 종단경사(비율)
        deg = math.degrees(math.atan(grade))
        data["slope"] = round(deg, 3)
        data["elev_start"] = round(e1, 1)
        data["elev_end"] = round(e2, 1)
        filled += 1
        steepest = max(steepest, deg)

    total = G.number_of_edges()
    return {
        "edges": total,
        "slope_filled": filled,
        "coverage": round(filled / total, 4) if total else 0.0,
        "max_slope_deg": round(steepest, 2),
        "source": "terrain-tiles(z%d)" % zoom,
    }
