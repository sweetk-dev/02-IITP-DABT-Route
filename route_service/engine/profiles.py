# -*- coding: utf-8 -*-
"""장애 유형별 통행 프로필.

기존 planning.py 는 max_slope=4.0 상수 하나로만 동작했다. 실제 서비스는
이용자 특성에 따라 통행 가능 경사·회피 대상·최소 보도폭·보행 속도가 달라지므로
프로필로 분리한다.

- max_slope_deg : 권장 경사 상한. 넘는 링크는 비용을 가중(slope_over_penalty)하고 경고를 붙인다
- hard_slope_deg: 이 값을 넘는 링크는 통행 불가(하드 필터). 0 이면 max_slope_deg 와 같다.
  v1.20.0 — 종전에는 max_slope_deg 가 하드 필터였다. 등고선 기반 5m DEM 은 짧은 링크에서 경사가 튀어
  실제로는 평탄한 직행로(안양문화원→안양세무서 정류장 205m)가 잘리고 649m 우회가 나왔다(실증 2026-09-03).
- avoid         : 통행 불가 link_type (계단·육교 등)
- penalize      : 통행은 가능하나 비용을 가중하는 link_type -> 가중치 배수
- min_width_m   : 유효 보도폭 하한(데이터에 width 가 있을 때만 적용)
- speed_mps     : 소요시간 추정용 평균 이동속도
- slope_factor  : 비용 = length * (1 + slope_factor * slope_deg)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    max_slope_deg: float
    speed_mps: float
    slope_factor: float
    avoid: tuple = ()
    penalize: dict = field(default_factory=dict)
    min_width_m: float = 0.0
    requires_curb_cut: bool = False
    hard_slope_deg: float = 0.0          # 0 = max_slope_deg 와 동일 (v1.20.0)
    slope_over_penalty: float = 1.0      # 권장 초과 1도당 비용 가중 배수 증가분 (v1.20.0)

    def hard_slope(self) -> float:
        """통행 불가 경사 상한 — 권장 상한보다 낮게는 잡히지 않는다(제약으로 max 를 올리면 같이 올라간다)."""
        return max(float(self.hard_slope_deg or 0.0), float(self.max_slope_deg))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "max_slope_deg": self.max_slope_deg,
            "hard_slope_deg": self.hard_slope(),
            "speed_mps": self.speed_mps,
            "avoid": list(self.avoid),
            "min_width_m": self.min_width_m,
            "requires_curb_cut": self.requires_curb_cut,
        }


PROFILES = {
    "wheelchair_manual": Profile(
        id="wheelchair_manual",
        label="수동 휠체어",
        max_slope_deg=4.0,
        speed_mps=0.7,
        slope_factor=0.30,
        avoid=("steps", "overpass", "underpass"),
        # road 가중: 보도(sidewalk)가 있는 우회로가 있으면 이면도로·차도 통행을
        # 뒤로 미룬다. 하드 차단이 아니라 가중이므로 보도 미비 구간은 여전히 통행 가능.
        penalize={"crossing": 1.2, "ramp": 1.1, "road": 1.25, "unknown": 1.1},
        min_width_m=0.9,
        requires_curb_cut=True,
        hard_slope_deg=8.0,
        slope_over_penalty=1.0,
    ),
    "wheelchair_electric": Profile(
        id="wheelchair_electric",
        label="전동 휠체어",
        max_slope_deg=6.0,
        speed_mps=1.1,
        slope_factor=0.15,
        avoid=("steps", "overpass", "underpass"),
        penalize={"crossing": 1.1, "road": 1.2, "unknown": 1.1},
        min_width_m=0.9,
        requires_curb_cut=True,
        hard_slope_deg=10.0,
        slope_over_penalty=0.6,
    ),
    "crutch": Profile(
        id="crutch",
        label="목발·보행보조",
        max_slope_deg=8.0,
        speed_mps=0.6,
        slope_factor=0.20,
        avoid=("overpass",),
        penalize={"steps": 3.0},
        min_width_m=0.8,
    ),
    "visual": Profile(
        id="visual",
        label="시각장애",
        max_slope_deg=12.0,
        speed_mps=0.8,
        slope_factor=0.05,
        avoid=(),
        penalize={"crossing": 1.5, "overpass": 2.0, "underpass": 1.5},
        min_width_m=0.0,
    ),
    "walk": Profile(
        id="walk",
        label="일반 보행",
        max_slope_deg=20.0,
        speed_mps=1.2,
        slope_factor=0.05,
        avoid=(),
        penalize={},
        min_width_m=0.0,
    ),
}

DEFAULT_PROFILE = "wheelchair_manual"


def get_profile(profile_id):
    """프로필 조회. 미지정/미등록이면 기본 프로필(수동 휠체어)."""
    if not profile_id:
        return PROFILES[DEFAULT_PROFILE]
    p = PROFILES.get(profile_id)
    if p is None:
        raise KeyError(profile_id)
    return p


def list_profiles() -> list:
    return [p.to_dict() for p in PROFILES.values()]
