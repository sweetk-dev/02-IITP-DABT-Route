# -*- coding: utf-8 -*-
"""환경 설정 — 모든 값은 환경변수로 주입한다(레포에 값 하드코딩 금지)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split(v: str) -> list:
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Settings:
    # 네트워크 그래프
    network_path: str = os.environ.get("NETWORK_PATH", "data/network.gpickle")
    network_version: str = os.environ.get("NETWORK_VERSION", "unknown")
    region_name: str = os.environ.get("REGION_NAME", "안양시")

    # POI 백엔드: db | file | none
    poi_backend: str = os.environ.get("POI_BACKEND", "none")
    poi_data_dir: str = os.environ.get("POI_DATA_DIR", "data/poi")
    poi_db_dsn: str = os.environ.get("POI_DB_DSN", "")

    # 목적지 접근점(무장애 출입구) 해석
    buildings_path: str = os.environ.get("BUILDINGS_PATH", "data/buildings_anyang.pkl")
    entrances_path: str = os.environ.get("ENTRANCES_PATH", "data/poi/entrances.json")
    entrance_max_walk_m: float = float(os.environ.get("ENTRANCE_MAX_WALK_M", "120"))

    # 서비스
    api_token: str = os.environ.get("ROUTE_API_TOKEN", "")
    allowed_origins: list = field(
        default_factory=lambda: _split(
            os.environ.get("ALLOWED_ORIGINS", "http://127.0.0.1:18000,http://localhost:18000")
        )
    )

    # 실시간 버스(GBIS, 공공데이터포털 인증키 하나로 도착·위치 API 모두 호출)
    gbis_api_key: str = os.environ.get("DATA_GO_KR_API_KEY", os.environ.get("GBIS_API_KEY", ""))
    gbis_base_url: str = os.environ.get("GBIS_BASE_URL", "https://apis.data.go.kr/6410000")
    gbis_timeout_sec: float = float(os.environ.get("GBIS_TIMEOUT_SEC", "3"))
    gbis_cache_ttl_sec: float = float(os.environ.get("GBIS_CACHE_TTL_SEC", "20"))

    # 탐색 파라미터
    snap_max_dist_m: float = float(os.environ.get("SNAP_MAX_DIST_M", "300"))
    max_alternatives: int = int(os.environ.get("MAX_ALTERNATIVES", "2"))
    off_route_threshold_m: float = float(os.environ.get("OFF_ROUTE_THRESHOLD_M", "30"))


_settings = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
