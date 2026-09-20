# -*- coding: utf-8 -*-
"""요청 처리시간·이벤트 계측 (#73).

실증 정량지표 ②(응답시간)·③(관광 추천 정확도)의 원천 로그다. 세 가지를 남긴다.

  · request   — 모든 API 요청의 서버 내부 처리시간(ms). 응답 헤더 X-Process-Time-Ms 로도 노출
  · reroute   — /route/reroute 호출 시 이탈 거리·이전/신규 route_id
  · recommend — /tour/recommend 의 요청 조건과 결과 스냅샷(poi_id·score 순서)

저장은 JSONL 파일(append, 1행 1이벤트)이다. DB 스키마를 건드리지 않고 볼륨에 남겨
실증 후 배치로 P95·MAP 을 재현한다. 파일을 못 열면 계측만 건너뛰고 서비스는 계속한다.
최근 N 건은 메모리에도 유지해 ``/meta/latency`` 가 즉시 요약한다.

핸들러가 ``tag(profile=..., mode=...)`` 로 문맥을 붙이면 미들웨어가 request 행에 합친다.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger("route_api.metrics")

_TAGS: contextvars.ContextVar = contextvars.ContextVar("route_metrics_tags", default=None)

RECENT_MAX = 5000


class Metrics:
    def __init__(self, path: str = "", enabled: bool = True):
        self.path = path or ""
        self.enabled = bool(enabled)
        self.recent = deque(maxlen=RECENT_MAX)
        self._lock = threading.Lock()
        self._fh = None
        self.dropped = 0

    # ── 기록 ──
    def _open(self):
        if self._fh is not None or not self.path:
            return self._fh
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError as e:
            logger.warning("계측 로그 파일을 열 수 없어 파일 기록을 건너뛴다(%s): %s", self.path, e)
            self.path = ""
        return self._fh

    def write(self, kind: str, **fields):
        if not self.enabled:
            return
        rec = {"kind": kind, "ts": round(time.time(), 3)}
        rec.update({k: v for k, v in fields.items() if v is not None})
        with self._lock:
            self.recent.append(rec)
            fh = self._open()
            if fh is not None:
                try:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                except (OSError, ValueError):
                    self.dropped += 1

    def close(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None

    # ── 요약 ──
    def summary(self, since_sec: float = 0.0) -> dict:
        """경로(path)별 건수·평균·P50·P95(ms). since_sec 이 0 이면 메모리 보유분 전부."""
        cutoff = time.time() - since_sec if since_sec and since_sec > 0 else 0
        buckets = {}
        with self._lock:
            rows = [r for r in self.recent if r.get("kind") == "request" and r["ts"] >= cutoff]
        for r in rows:
            buckets.setdefault(r.get("path"), []).append(float(r.get("ms", 0)))
        out = {}
        for path, xs in buckets.items():
            xs.sort()
            n = len(xs)
            out[path] = {
                "count": n,
                "avg_ms": round(sum(xs) / n, 1),
                "p50_ms": round(xs[int(0.50 * (n - 1))], 1),
                "p95_ms": round(xs[int(0.95 * (n - 1))], 1),
                "max_ms": round(xs[-1], 1),
                "over_3s": sum(1 for x in xs if x > 3000),
            }
        return {"since_sec": since_sec, "paths": out, "recent_kept": len(self.recent),
                "file": self.path or None, "dropped": self.dropped}


METRICS = Metrics(enabled=False)


def configure(settings) -> Metrics:
    global METRICS
    METRICS = Metrics(path=getattr(settings, "metrics_log_path", ""),
                      enabled=getattr(settings, "metrics_enabled", True))
    return METRICS


def tag(**kv):
    """핸들러에서 현재 요청 행에 붙일 문맥(profile·mode·route_id 등)을 등록한다.

    동기 핸들러는 스레드풀에서 **복사된** 컨텍스트로 돌기 때문에 ContextVar 를 다시 set 하면
    미들웨어 쪽에는 보이지 않는다. 그래서 미들웨어가 요청마다 만든 dict 를 그 자리에서
    갱신(in-place)한다 — 객체는 복사본과 원본이 공유한다.
    """
    cur = _TAGS.get()
    if cur is None:                      # 미들웨어 밖(단위 테스트 등)에서는 무시
        return
    cur.update({k: v for k, v in kv.items() if v is not None})


def reset_tags():
    _TAGS.set({})


def current_tags() -> dict:
    return dict(_TAGS.get() or {})
