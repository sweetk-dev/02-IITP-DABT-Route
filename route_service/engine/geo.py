# -*- coding: utf-8 -*-
"""좌표 계산 유틸 (외부 의존 없음)."""
from __future__ import annotations

import math

EARTH_R = 6371008.8  # m


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 간 대권 거리(m)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """진행 방위각(0=북, 시계방향)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def turn_angle(prev_bearing: float, next_bearing: float) -> float:
    """직전 진행방향 대비 회전각(-180~180, 양수=우회전)."""
    d = (next_bearing - prev_bearing + 540.0) % 360.0 - 180.0
    return d


def lead_bearing(coords, span_m: float = 10.0) -> float:
    """링크 진입 방위각 — 시작점에서 span_m 진행한 지점까지로 잰다 (v1.21.0).

    종전에는 coords[0]->coords[1] 두 점만 썼다. 수치지형도 인도 면형을 중심선화하면
    교차로 모서리에 1~2m 짜리 미세 절점이 흔히 생기는데, 그 조각의 방위각은 실제
    진행 방향과 무관하게 크게 튄다. 그 값으로 회전을 판정하니 평범한 모퉁이가
    급좌회전·유턴으로 승격됐다(실증 2026-09-03, 안양문화원 앞 -137.7도).
    링크가 span_m 보다 짧으면 링크 전체로 잰다.
    """
    if len(coords) < 2:
        return 0.0
    a = coords[0]
    acc = 0.0
    for i in range(1, len(coords)):
        acc += haversine_m(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        if acc >= span_m:
            return bearing_deg(a[0], a[1], coords[i][0], coords[i][1])
    return bearing_deg(a[0], a[1], coords[-1][0], coords[-1][1])


def trail_bearing(coords, span_m: float = 10.0) -> float:
    """링크 이탈 방위각 — 끝점 직전 span_m 구간으로 잰다. lead_bearing 의 역방향."""
    if len(coords) < 2:
        return 0.0
    b = coords[-1]
    acc = 0.0
    for i in range(len(coords) - 2, -1, -1):
        acc += haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        if acc >= span_m:
            return bearing_deg(coords[i][0], coords[i][1], b[0], b[1])
    return bearing_deg(coords[0][0], coords[0][1], b[0], b[1])


def path_length_m(coords) -> float:
    total = 0.0
    for (a_lat, a_lon), (b_lat, b_lon) in zip(coords[:-1], coords[1:]):
        total += haversine_m(a_lat, a_lon, b_lat, b_lon)
    return total


def point_segment_dist_m(plat, plon, alat, alon, blat, blon) -> float:
    """점-선분 최단거리(m). 소구역이라 평면 근사."""
    lat0 = math.radians((alat + blat) / 2.0)
    kx = math.cos(lat0) * 111320.0
    ky = 110540.0
    px, py = plon * kx, plat * ky
    ax, ay = alon * kx, alat * ky
    bx, by = blon * kx, blat * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)
