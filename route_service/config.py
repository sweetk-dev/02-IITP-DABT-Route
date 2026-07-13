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

    # 서비스
    api_token: str = os.environ.get("ROUTE_API_TOKEN", "")
    allowed_origins: list = field(
        default_factory=lambda: _split(
            os.environ.get("ALLOWED_ORIGINS", "http://127.0.0.1:18000,http://localhost:18000")
        )
    )

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
