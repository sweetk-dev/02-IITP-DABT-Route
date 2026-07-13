# -*- coding: utf-8 -*-
"""보행 네트워크 소스 어댑터.

- osm      : OpenStreetMap 보행망 (안양 선구축용)
- tabular  : node/link 표 형식 (인천 검증본, 융기원 제공 예정 데이터)

두 어댑터 모두 동일한 그래프 스키마(engine/graph.py 참조)를 산출하므로,
융기원 원본이 도착하면 API 변경 없이 그래프만 교체하면 된다.
"""
from .osm import build_from_osm
from .tabular import build_from_tabular

BUILDERS = {"osm": build_from_osm, "tabular": build_from_tabular}
