# -*- coding: utf-8 -*-
"""수치지형도(1:1,000) -> 보행망 구축 파이프라인.

국토지리정보원 수치지도 Ver2.0 (SHP) 및 NGI/NDA 교환포맷을 읽어
보도 중심선 + 위상(node/link)을 산출한다. 산출물은 engine.sources.tabular
규격을 따르므로 build_network.py --source tabular 로 바로 투입된다.
"""
from .extract import extract_sheet, SIDEWALK_AREA, SIDEWALK_LINE, OVERPASS, ROAD_CENTER
from .centerline import poly_to_centerlines
from .dissolve import dissolve_polys, grid_centerlines
from .topology import build_topology

__all__ = ["extract_sheet", "poly_to_centerlines", "build_topology", "grid_centerlines",
           "SIDEWALK_AREA", "SIDEWALK_LINE", "OVERPASS", "ROAD_CENTER"]
