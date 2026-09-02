# -*- coding: utf-8 -*-
"""경기버스정보(GBIS) 실시간 조회 — 정류장 도착정보 · 노선 차량 위치.

정적 데이터(01 `tran_bus_*`)에는 저상버스 정차 여부가 없다. 그래서 지금까지는
"실시간 도착정보로 확인하세요"라는 경고만 붙여 왔고, 확인은 이용자 몫이었다.
이 모듈이 그 확인을 서비스 안으로 가져온다.

- 도착정보 `getBusArrivalListv2?stationId=`  — 정류장 기준, 노선별 1·2번째 차량의
  도착 예정(분)·몇 정거장 전·**저상 여부(lowPlate)**·차량번호
- 위치정보 `getBusLocationListv2?routeId=`   — 노선 기준, 운행 중인 전 차량의
  현재 정류장 순번·저상 여부

인증키는 공공데이터포털 일반 인증키(`DATA_GO_KR_API_KEY`) 하나로 두 API 를 모두 부른다.
호출 실패는 예외로 올리지 않고 ``{"status": "unavailable", "reason": ...}`` 로 돌려준다 —
실시간을 못 붙였다고 경로 안내가 멈추면 안 된다.

응답 값의 빈 항목은 빈 문자열("")로 온다(실측 2026-09-02). 정수 변환은 전부 관대하게 한다.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("route_api.gbis")

ARRIVAL_PATH = "/busarrivalservice/v2/getBusArrivalListv2"
LOCATION_PATH = "/buslocationservice/v2/getBusLocationListv2"

# lowPlate: 0 일반 / 1 저상 / 2 2층 등 특수차량 — 휠체어 승차 기준으로는 1 만 '저상'이다
LOW_FLOOR_CODE = 1

# stateCd(위치정보): 0 교차로 통과 / 1 정류소 도착 / 2 정류소 출발
STATE_LABEL = {0: "이동 중", 1: "정류소 도착", 2: "정류소 출발"}


def _int(v, default=None):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


class GbisLive:
    """실시간 조회 클라이언트. 같은 정류장·노선을 짧은 간격으로 반복 조회하는 폴링을
    TTL 캐시로 흡수한다(개발계정 도착정보 한도 1,000회/일)."""

    def __init__(self, api_key: str = "", base_url: str = "https://apis.data.go.kr/6410000",
                 timeout_sec: float = 3.0, cache_ttl_sec: float = 20.0, fetch=None):
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.cache_ttl_sec = float(cache_ttl_sec)
        self._fetch = fetch or self._http_get
        self._cache = {}
        self._lock = threading.Lock()

    # ---------- 공통 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _http_get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "iitp-dabt-route/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as r:
            return json.loads(r.read().decode("utf-8"))

    def _url(self, path: str, **params) -> str:
        q = {"serviceKey": self.api_key, "format": "json"}
        q.update({k: v for k, v in params.items() if v not in (None, "")})
        return "%s%s?%s" % (self.base_url, path, urllib.parse.urlencode(q))

    def _get(self, key: tuple, path: str, **params):
        """캐시 → 호출. 반환: (msg_body dict | None, error str | None)."""
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1], hit[2]
        if not self.enabled:
            return None, "인증키가 설정되지 않았습니다(DATA_GO_KR_API_KEY)"
        body, err = None, None
        try:
            data = self._fetch(self._url(path, **params))
            resp = (data or {}).get("response") or {}
            head = resp.get("msgHeader") or {}
            code = _int(head.get("resultCode"), -1)
            if code == 0:
                body = resp.get("msgBody") or {}
            elif code == 4:
                body = {}                    # 결과 없음 — 정상 응답의 한 형태
            else:
                err = "GBIS resultCode=%s %s" % (code, head.get("resultMessage") or "")
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            err = "%s: %s" % (type(e).__name__, e)
        if err:
            logger.warning("GBIS 호출 실패 %s %s — %s", path, params, err)
        with self._lock:
            # 실패도 짧게 캐시한다 — 장애 중 폴링이 외부 API 를 두드리지 않게
            ttl = self.cache_ttl_sec if not err else min(self.cache_ttl_sec, 10.0)
            self._cache[key] = (now + ttl, body, err)
            if len(self._cache) > 500:
                self._cache.clear()
        return body, err

    # ---------- 도착정보 ----------
    @staticmethod
    def _vehicle(item: dict, n: int):
        plate = _str(item.get("plateNo%d" % n))
        pred = _int(item.get("predictTime%d" % n))
        if plate is None and pred is None:
            return None
        low = _int(item.get("lowPlate%d" % n))
        return {
            "order": n,
            "predict_min": pred,
            "predict_sec": _int(item.get("predictTimeSec%d" % n)),
            "stops_away": _int(item.get("locationNo%d" % n)),
            "current_stop": _str(item.get("stationNm%d" % n)),
            "low_floor": (low == LOW_FLOOR_CODE) if low is not None else None,
            "plate_no": plate,
            "remain_seat_cnt": _int(item.get("remainSeatCnt%d" % n)),
            "crowded": _int(item.get("crowded%d" % n)),
        }

    def arrivals(self, station_id, route_id=None, route_meta: dict = None) -> dict:
        """정류장 도착정보.

        route_meta: {route_id(str): {"name","type","end_station"}} — 정적 DB 의 노선명·유형을
        덧입힌다(도착 API 도 routeName 을 주지만 유형은 코드뿐이다).
        route_id 를 주면 그 노선만 남긴다(경로 안내 중 승차 노선 확인용).
        """
        body, err = self._get(("arr", str(station_id)), ARRIVAL_PATH, stationId=station_id)
        if err:
            return {"status": "unavailable", "reason": err, "station_id": str(station_id),
                    "items": [], "next_low_floor": None}
        raw = (body or {}).get("busArrivalList") or []
        if isinstance(raw, dict):
            raw = [raw]
        items = []
        for it in raw:
            rid = _str(it.get("routeId"))
            if route_id is not None and str(route_id) != rid:
                continue
            meta = (route_meta or {}).get(rid) or {}
            vehicles = [v for v in (self._vehicle(it, 1), self._vehicle(it, 2)) if v]
            items.append({
                "route_id": rid,
                "route_name": meta.get("name") or _str(it.get("routeName")),
                "route_type": meta.get("type"),
                "end_station": meta.get("end_station") or _str(it.get("routeDestName")),
                "station_seq": _int(it.get("staOrder")),
                "flag": _str(it.get("flag")),
                "vehicles": vehicles,
            })
        items.sort(key=lambda x: (x["vehicles"][0]["predict_min"] if x["vehicles"]
                                  and x["vehicles"][0]["predict_min"] is not None else 9999,
                                  x["route_name"] or ""))
        return {
            "status": "success",
            "station_id": str(station_id),
            "queried_at": int(time.time()),
            "items": items,
            "next_low_floor": self.next_low_floor(items),
        }

    @staticmethod
    def next_low_floor(items: list):
        """가장 빨리 오는 저상 차량 하나. 없으면 None."""
        best = None
        for it in items:
            for v in it.get("vehicles") or []:
                if not v.get("low_floor"):
                    continue
                pm = v.get("predict_min")
                if pm is None:
                    continue
                cand = {"route_id": it["route_id"], "route_name": it.get("route_name"),
                        "route_type": it.get("route_type"), "end_station": it.get("end_station"),
                        "predict_min": pm, "stops_away": v.get("stops_away"),
                        "plate_no": v.get("plate_no")}
                if best is None or pm < best["predict_min"]:
                    best = cand
        return best

    # ---------- 위치정보 ----------
    def locations(self, route_id, stop_index: dict = None) -> dict:
        """노선의 운행 차량 위치. stop_index: {station_id(str): {"name","lat","lng","station_seq"}}
        가 있으면 차량이 있는 정류장의 이름·좌표를 붙인다(지도 표시용)."""
        body, err = self._get(("loc", str(route_id)), LOCATION_PATH, routeId=route_id)
        if err:
            return {"status": "unavailable", "reason": err, "route_id": str(route_id),
                    "vehicles": [], "low_floor_cnt": 0}
        raw = (body or {}).get("busLocationList") or []
        if isinstance(raw, dict):
            raw = [raw]
        vehicles = []
        for it in raw:
            sid = _str(it.get("stationId"))
            low = _int(it.get("lowPlate"))
            st = _int(it.get("stateCd"))
            v = {
                "vehicle_id": _str(it.get("vehId")),
                "plate_no": _str(it.get("plateNo")),
                "low_floor": (low == LOW_FLOOR_CODE) if low is not None else None,
                "station_id": sid,
                "station_seq": _int(it.get("stationSeq")),
                "state": STATE_LABEL.get(st, None),
                "remain_seat_cnt": _int(it.get("remainSeatCnt")),
                "crowded": _int(it.get("crowded")),
            }
            info = (stop_index or {}).get(sid)
            if info:
                v["station_name"] = info.get("name")
                v["lat"] = info.get("lat")
                v["lng"] = info.get("lng")
            vehicles.append(v)
        vehicles.sort(key=lambda x: (x["station_seq"] if x["station_seq"] is not None else 9999))
        return {
            "status": "success",
            "route_id": str(route_id),
            "queried_at": int(time.time()),
            "vehicles": vehicles,
            "low_floor_cnt": sum(1 for v in vehicles if v["low_floor"]),
        }


def configure(settings) -> GbisLive:
    return GbisLive(api_key=settings.gbis_api_key, base_url=settings.gbis_base_url,
                    timeout_sec=settings.gbis_timeout_sec,
                    cache_ttl_sec=settings.gbis_cache_ttl_sec)


LIVE = GbisLive()
