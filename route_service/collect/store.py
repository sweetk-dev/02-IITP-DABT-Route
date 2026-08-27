# -*- coding: utf-8 -*-
"""트랙·제보·오버라이드 저장소.

backend
  db     : POI_DB_DSN(iitp_db) 재사용. 스키마는 scripts/sql/collect_schema.sql
  memory : DSN 미설정·테스트용 인메모리 (프로세스 생존 동안만 유지)

개인정보: 참여자 식별자는 받지도 저장하지도 않는다. route_id(익명 난수) 단위.

품질 개선 자동화 수위 (사용자 확정 2026-08-27)
  1) 경고는 자동   — 제보 접수 즉시 warning 오버라이드 생성 ('이용자 제보(미확인)')
  2) 속성은 승인제 — 관리자 confirm/apply 를 거쳐야 curb_cut 등 실제 속성 오버라이드 생성
  3) 좌표 앵커     — 오버라이드는 노드/링크 ID 가 아닌 좌표+반경. 그래프 재생성에도 생존
"""
from __future__ import annotations

import base64
import threading
from datetime import datetime, timezone

REASONS = {
    "curb": "턱 있음",
    "no_sidewalk": "보도 없음·끊김",
    "no_crossing": "횡단보도 없음",
    "steep": "경사 심함",
    "blocked": "통행 불가(공사 등)",
    "etc": "기타",
}
REPORT_STATUSES = ("new", "confirmed", "rejected", "applied")
OVERRIDE_ATTRS = ("warning", "curb_cut", "tactile_paving", "width", "passable")
# 승인제 속성 — 관리자 액션(apply)으로만 생성 가능
APPROVAL_ONLY_ATTRS = ("curb_cut", "tactile_paving", "width", "passable")
MAX_POINTS_PER_CALL = 5000
MAX_PHOTO_BYTES = 2 * 1024 * 1024


def warning_text(reason: str, confirmed: bool = False) -> str:
    label = REASONS.get(reason, REASONS["etc"])
    return "이용자 제보: %s%s" % (label, "" if confirmed else " (미확인)")


class CollectStore:
    def __init__(self, dsn: str = ""):
        self.dsn = dsn
        self.backend = "db" if dsn else "memory"
        self._engine = None
        self._lock = threading.Lock()
        # memory backend
        self._track_meta = {}
        self._track_points = {}       # route_id -> {seq: point}
        self._reports = {}            # report_id -> dict
        self._overrides = {}          # override_id -> dict
        self._next_report = 1
        self._next_override = 1

    # ---------- db helpers ----------
    def _db(self):
        if self._engine is None:
            from sqlalchemy import create_engine
            self._engine = create_engine(self.dsn, pool_pre_ping=True, future=True)
        return self._engine

    def _exec(self, sql: str, params=None, fetch: bool = False):
        from sqlalchemy import text
        with self._db().begin() as conn:
            res = conn.execute(text(sql), params or {})
            if fetch:
                return [dict(r) for r in res.mappings().all()]
            return None

    # ---------- 트랙 ----------
    def log_track(self, route_id: str, points: list, meta: dict | None) -> int:
        points = points[:MAX_POINTS_PER_CALL]
        meta = meta or {}
        if self.backend == "memory":
            with self._lock:
                m = self._track_meta.setdefault(route_id, {"route_id": route_id, "point_cnt": 0})
                for k in ("profile", "network_version", "planned_dist_m", "geometry",
                          "outcome", "started_at", "finished_at"):
                    if meta.get(k) is not None:
                        m[k] = meta[k]
                bucket = self._track_points.setdefault(route_id, {})
                for p in points:
                    bucket.setdefault(int(p["seq"]), p)
                m["point_cnt"] = len(bucket)
                return len(bucket)

        self._exec(
            """
            INSERT INTO mv_route_track_meta
                (route_id, profile, network_version, planned_dist_m, geometry,
                 started_at, finished_at, outcome)
            VALUES (:rid, :profile, :nv, :dist, CAST(:geom AS jsonb), :st, :ft, :outcome)
            ON CONFLICT (route_id) DO UPDATE SET
                profile         = COALESCE(EXCLUDED.profile, mv_route_track_meta.profile),
                network_version = COALESCE(EXCLUDED.network_version, mv_route_track_meta.network_version),
                planned_dist_m  = COALESCE(EXCLUDED.planned_dist_m, mv_route_track_meta.planned_dist_m),
                geometry        = COALESCE(EXCLUDED.geometry, mv_route_track_meta.geometry),
                started_at      = COALESCE(EXCLUDED.started_at, mv_route_track_meta.started_at),
                finished_at     = COALESCE(EXCLUDED.finished_at, mv_route_track_meta.finished_at),
                outcome         = COALESCE(EXCLUDED.outcome, mv_route_track_meta.outcome)
            """,
            {"rid": route_id, "profile": meta.get("profile"),
             "nv": meta.get("network_version"), "dist": meta.get("planned_dist_m"),
             "geom": __import__("json").dumps(meta["geometry"]) if meta.get("geometry") else None,
             "st": meta.get("started_at"), "ft": meta.get("finished_at"),
             "outcome": meta.get("outcome")},
        )
        for p in points:
            self._exec(
                """
                INSERT INTO mv_route_track (route_id, seq, lat, lon, ts, accuracy_m)
                VALUES (:rid, :seq, :lat, :lon, :ts, :acc)
                ON CONFLICT (route_id, seq) DO NOTHING
                """,
                {"rid": route_id, "seq": int(p["seq"]), "lat": float(p["lat"]),
                 "lon": float(p["lng"]), "ts": p.get("ts"), "acc": p.get("acc")},
            )
        rows = self._exec(
            """
            UPDATE mv_route_track_meta m
               SET point_cnt = (SELECT count(*) FROM mv_route_track t WHERE t.route_id = :rid)
             WHERE m.route_id = :rid
            RETURNING m.point_cnt
            """,
            {"rid": route_id}, fetch=True)
        return int(rows[0]["point_cnt"]) if rows else 0

    # ---------- 제보 ----------
    def add_report(self, lat: float, lon: float, reason: str, detail: str | None,
                   route_id: str | None, photo_b64: str | None, photo_mime: str | None) -> dict:
        if reason not in REASONS:
            reason = "etc"
        photo = None
        if photo_b64:
            photo = base64.b64decode(photo_b64)
            if len(photo) > MAX_PHOTO_BYTES:
                raise ValueError("사진이 너무 큽니다 (2MB 이하)")

        if self.backend == "memory":
            with self._lock:
                rid = self._next_report
                self._next_report += 1
                self._reports[rid] = {
                    "report_id": rid, "lat": lat, "lon": lon, "reason": reason,
                    "detail": detail, "route_id": route_id, "photo": photo,
                    "photo_mime": photo_mime, "status": "new", "review_note": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
        else:
            rows = self._exec(
                """
                INSERT INTO mv_access_report (lat, lon, reason, detail, route_id, photo, photo_mime)
                VALUES (:lat, :lon, :reason, :detail, :rid, :photo, :mime)
                RETURNING report_id
                """,
                {"lat": lat, "lon": lon, "reason": reason, "detail": detail,
                 "rid": route_id, "photo": photo, "mime": photo_mime}, fetch=True)
            rid = int(rows[0]["report_id"])

        # 자동화 수위 1: 경고 오버라이드는 접수 즉시 생성 (미확인 표기)
        ov_id = self.add_override(lat, lon, attr="warning",
                                  value=warning_text(reason), source="report",
                                  report_id=rid, note="제보 접수 자동 생성")
        return {"report_id": rid, "override_id": ov_id}

    def list_reports(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list:
        if self.backend == "memory":
            rows = sorted(self._reports.values(), key=lambda r: -r["report_id"])
            if status:
                rows = [r for r in rows if r["status"] == status]
            out = []
            for r in rows[offset:offset + limit]:
                d = {k: v for k, v in r.items() if k != "photo"}
                d["has_photo"] = r.get("photo") is not None
                out.append(d)
            return out
        return self._exec(
            """
            SELECT report_id, lat, lon, reason, detail, route_id, photo_mime,
                   (photo IS NOT NULL) AS has_photo, status, review_note,
                   reviewed_at, created_at
              FROM mv_access_report
             WHERE (:status IS NULL OR status = :status)
             ORDER BY report_id DESC
             LIMIT :limit OFFSET :offset
            """,
            {"status": status, "limit": limit, "offset": offset}, fetch=True)

    def get_report_photo(self, report_id: int):
        if self.backend == "memory":
            r = self._reports.get(report_id)
            return (r.get("photo"), r.get("photo_mime")) if r else (None, None)
        rows = self._exec(
            "SELECT photo, photo_mime FROM mv_access_report WHERE report_id = :id",
            {"id": report_id}, fetch=True)
        if not rows:
            return None, None
        return rows[0]["photo"], rows[0]["photo_mime"]

    def review_report(self, report_id: int, action: str, attr: str | None = None,
                      value: str | None = None, note: str | None = None,
                      radius_m: float = 20.0) -> dict:
        """관리자 검토 — confirm / reject / apply.

        confirm : 사실 확인. 경고 오버라이드를 확정 문구로 갱신(미확인 꼬리표 제거)
        reject  : 기각. 연결된 오버라이드 전부 철회
        apply   : 속성 반영(승인제). attr/value 로 속성 오버라이드 생성 + 경고는 철회
        """
        rep = self._get_report(report_id)
        if rep is None:
            raise KeyError("제보 %s 없음" % report_id)

        if action == "confirm":
            self._retire_report_overrides(report_id)
            self.add_override(rep["lat"], rep["lon"], attr="warning",
                              value=warning_text(rep["reason"], confirmed=True),
                              source="report", report_id=report_id, note=note or "관리자 확인")
            self._set_report_status(report_id, "confirmed", note)
        elif action == "reject":
            self._retire_report_overrides(report_id)
            self._set_report_status(report_id, "rejected", note)
        elif action == "apply":
            if attr not in APPROVAL_ONLY_ATTRS:
                raise ValueError("apply 가능한 속성: %s" % ", ".join(APPROVAL_ONLY_ATTRS))
            if value is None:
                raise ValueError("value 필요")
            self._retire_report_overrides(report_id)
            self.add_override(rep["lat"], rep["lon"], attr=attr, value=str(value),
                              source="report", report_id=report_id,
                              note=note or "관리자 속성 반영", radius_m=radius_m)
            self._set_report_status(report_id, "applied", note)
        else:
            raise ValueError("action 은 confirm | reject | apply")
        return {"report_id": report_id, "action": action}

    def _get_report(self, report_id: int):
        if self.backend == "memory":
            return self._reports.get(report_id)
        rows = self._exec(
            "SELECT report_id, lat, lon, reason, status FROM mv_access_report WHERE report_id = :id",
            {"id": report_id}, fetch=True)
        return rows[0] if rows else None

    def _set_report_status(self, report_id: int, status: str, note: str | None):
        if self.backend == "memory":
            r = self._reports[report_id]
            r["status"] = status
            r["review_note"] = note
            r["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            return
        self._exec(
            """
            UPDATE mv_access_report
               SET status = :status, review_note = :note, reviewed_at = now()
             WHERE report_id = :id
            """,
            {"status": status, "note": note, "id": report_id})

    # ---------- 오버라이드 ----------
    def add_override(self, lat: float, lon: float, attr: str, value: str,
                     source: str = "report", report_id: int | None = None,
                     note: str | None = None, radius_m: float = 20.0,
                     target: str = "link") -> int:
        if attr not in OVERRIDE_ATTRS:
            raise ValueError("attr 은 %s 중 하나" % ", ".join(OVERRIDE_ATTRS))
        if self.backend == "memory":
            with self._lock:
                oid = self._next_override
                self._next_override += 1
                self._overrides[oid] = {
                    "override_id": oid, "lat": lat, "lon": lon, "radius_m": radius_m,
                    "target": target, "attr": attr, "value": value, "source": source,
                    "report_id": report_id, "status": "active", "note": note,
                }
            return oid
        rows = self._exec(
            """
            INSERT INTO mv_access_override
                (lat, lon, radius_m, target, attr, value, source, report_id, note)
            VALUES (:lat, :lon, :radius, :target, :attr, :value, :source, :rid, :note)
            RETURNING override_id
            """,
            {"lat": lat, "lon": lon, "radius": radius_m, "target": target,
             "attr": attr, "value": value, "source": source, "rid": report_id,
             "note": note}, fetch=True)
        return int(rows[0]["override_id"])

    def _retire_report_overrides(self, report_id: int):
        if self.backend == "memory":
            for ov in self._overrides.values():
                if ov.get("report_id") == report_id:
                    ov["status"] = "retired"
            return
        self._exec(
            "UPDATE mv_access_override SET status = 'retired' WHERE report_id = :id",
            {"id": report_id})

    def active_overrides(self) -> list:
        if self.backend == "memory":
            return [dict(o) for o in self._overrides.values() if o["status"] == "active"]
        return self._exec(
            """
            SELECT override_id, lat, lon, radius_m, target, attr, value, source, report_id
              FROM mv_access_override WHERE status = 'active' ORDER BY override_id
            """, fetch=True)


STORE = CollectStore()


def configure(settings):
    global STORE
    STORE = CollectStore(dsn=settings.poi_db_dsn or "")
    return STORE
