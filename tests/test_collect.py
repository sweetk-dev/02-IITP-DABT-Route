# -*- coding: utf-8 -*-
"""수집 장치화 (v1.16.0) — 트랙·제보·오버라이드.

자동화 수위 계약:
  1) 제보 접수 즉시 warning 오버라이드('미확인') — 안내 경고에 자동 노출
  2) 속성 변경(curb_cut 등)은 관리자 apply 로만 생성
  3) 오버라이드는 좌표 앵커 — 그래프 재적용(revert+apply)이 멱등
"""
from __future__ import annotations

import networkx as nx
import pytest

from route_service.collect.store import CollectStore
from route_service.engine.graph import NetworkStore
from route_service.engine.overrides import apply_overrides
from route_service.engine.planner import edge_passable, plan, NoRouteError
from route_service.engine.profiles import get_profile
from route_service.engine.steps import build_steps
from scripts.analyze_tracks import analyze_route


def _graph() -> nx.Graph:
    G = nx.Graph()
    G.add_node("N1", lat=37.3900, lon=126.9500, node_type="intersection")
    G.add_node("N2", lat=37.3900, lon=126.9511, node_type="intersection")
    G.add_node("N3", lat=37.3909, lon=126.9511, node_type="entrance")
    G.add_edge("N1", "N2", length=100.0, slope=1.0, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name="예시로", geometry=None)
    G.add_edge("N2", "N3", length=100.0, slope=1.5, link_type="sidewalk",
               width=2.0, curb_cut=True, surface=None, link_name="예시2로", geometry=None)
    return G


def _store(G):
    s = NetworkStore()
    s.load_graph_object(G, version="t", region="")
    return s


# ───────────── CollectStore (memory backend) ─────────────

def test_report_auto_creates_unconfirmed_warning_override():
    cs = CollectStore()
    out = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)
    assert out["report_id"] == 1
    ovs = cs.active_overrides()
    assert len(ovs) == 1
    assert ovs[0]["attr"] == "warning"
    assert "미확인" in ovs[0]["value"]


def test_reject_retires_override():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)["report_id"]
    cs.review_report(rid, "reject", note="현장 확인 결과 이상 없음")
    assert cs.active_overrides() == []
    assert cs.list_reports()[0]["status"] == "rejected"


def test_confirm_replaces_with_confirmed_warning():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "steep", None, None, None, None)["report_id"]
    cs.review_report(rid, "confirm")
    ovs = cs.active_overrides()
    assert len(ovs) == 1
    assert "미확인" not in ovs[0]["value"]


def test_apply_is_approval_only_and_creates_attr_override():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)["report_id"]
    with pytest.raises(ValueError):
        cs.review_report(rid, "apply", attr="warning", value="x")   # 승인제 목록 밖
    cs.review_report(rid, "apply", attr="curb_cut", value="false")
    ovs = cs.active_overrides()
    assert len(ovs) == 1
    assert ovs[0]["attr"] == "curb_cut" and ovs[0]["value"] == "false"
    assert cs.list_reports()[0]["status"] == "applied"


def test_delete_report_removes_record_and_its_overrides():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)["report_id"]
    assert len(cs.active_overrides()) == 1

    out = cs.delete_report(rid)
    assert out["deleted"] is True and out["deleted_overrides"] == 1
    assert cs.list_reports() == []
    # 기각과 달리 흔적이 남지 않는다 — retired 도 아니고 아예 없다
    assert cs._overrides == {}


def test_delete_report_after_apply_also_drops_attr_override():
    cs = CollectStore()
    rid = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)["report_id"]
    cs.review_report(rid, "apply", attr="curb_cut", value="false")
    assert len(cs.active_overrides()) == 1

    cs.delete_report(rid)
    assert cs.active_overrides() == []
    assert cs.list_reports() == []


def test_delete_unknown_report_raises():
    cs = CollectStore()
    with pytest.raises(KeyError):
        cs.delete_report(999)


def test_delete_leaves_other_reports_untouched():
    cs = CollectStore()
    keep = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)["report_id"]
    drop = cs.add_report(37.3902, 126.9507, "steep", None, None, None, None)["report_id"]
    cs.delete_report(drop)
    ids = [r["report_id"] for r in cs.list_reports()]
    assert ids == [keep]
    assert len(cs.active_overrides()) == 1


def test_track_log_dedupes_by_seq():
    cs = CollectStore()
    pts = [{"seq": 0, "lat": 37.39, "lng": 126.95},
           {"seq": 1, "lat": 37.3901, "lng": 126.9501}]
    assert cs.log_track("r_x", pts, {"planned_dist_m": 200}) == 2
    assert cs.log_track("r_x", pts, None) == 2      # 재업로드 멱등


# ───────────── 오버라이드 그래프 적용 ─────────────

def test_warning_override_reaches_steps_and_summary():
    G = _graph()
    # N1-N2 링크 중간쯤 좌표
    ovs = [{"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "warning", "value": "이용자 제보: 턱 있음 (미확인)"}]
    stat = apply_overrides(G, ovs)
    assert stat["warnings"] == 1
    store = _store(G)
    p = get_profile("wheelchair_manual")
    r = plan(store, "N1", "N3", p)["routes"][0]
    assert "이용자 제보: 턱 있음 (미확인)" in r["summary"]["warnings"]
    steps = build_steps(store.graph, r["path"], p)
    assert any("이용자 제보" in w for s in steps for w in s["warnings"])


def test_passable_false_blocks_routing():
    G = _graph()
    ovs = [{"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "passable", "value": "false"}]
    apply_overrides(G, ovs)
    d = G["N1"]["N2"]
    assert d.get("blocked") is True
    assert edge_passable(d, get_profile("walk"), 20.0) is False
    with pytest.raises(NoRouteError):
        plan(_store(G), "N1", "N3", get_profile("walk"), relax=False)


def test_reapply_is_idempotent_and_revertible():
    G = _graph()
    ovs = [{"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "curb_cut", "value": "false"},
           {"lat": 37.3900, "lon": 126.9505, "radius_m": 20,
            "attr": "warning", "value": "경고"}]
    apply_overrides(G, ovs)
    assert G["N1"]["N2"]["curb_cut"] is False
    apply_overrides(G, ovs)                        # 재적용해도 동일
    assert G["N1"]["N2"]["curb_cut"] is False
    assert G["N1"]["N2"]["report_warnings"] == ["경고"]
    stat = apply_overrides(G, [])                  # 전부 철회
    assert stat["applied"] == 0
    assert G["N1"]["N2"]["curb_cut"] is True       # 원복
    assert "report_warnings" not in G["N1"]["N2"]


def test_override_outside_radius_is_unmatched():
    G = _graph()
    ovs = [{"lat": 37.5000, "lon": 127.1000, "radius_m": 20,
            "attr": "warning", "value": "멀리"}]
    stat = apply_overrides(G, ovs)
    assert stat["unmatched"] == 1 and stat["applied"] == 0


# ───────────── 트랙 분석 ─────────────

def test_track_analysis_detects_honest_and_off_route():
    geom = [[37.3900, 126.9500], [37.3900, 126.9511], [37.3909, 126.9511]]
    on_route = [{"seq": i, "lat": 37.3900, "lng": 126.9500 + i * 0.0001}
                for i in range(12)]
    r = analyze_route("r_ok", geom, 200, on_route)
    assert r["off_route_clusters"] == []

    off = [{"seq": i, "lat": 37.3930, "lng": 126.9500 + i * 0.0001}   # 300m 북쪽
           for i in range(12)]
    r2 = analyze_route("r_off", geom, 200, off)
    assert len(r2["off_route_clusters"]) >= 1
    assert r2["off_route_clusters"][0]["max_off_m"] > 100


# ───────────── API 계약 (memory backend) ─────────────

def test_collect_api_end_to_end(tmp_path, monkeypatch):
    import json as _json
    import pickle
    from fastapi.testclient import TestClient

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(_graph(), f)
    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-collect")
    monkeypatch.setenv("POI_BACKEND", "none")
    monkeypatch.setenv("POI_DB_DSN", "")
    monkeypatch.setenv("ROUTE_API_TOKEN", "")

    import route_service.config as config
    config._settings = None
    import importlib
    import route_service.api.main as main
    importlib.reload(main)

    with TestClient(main.app) as client:
        # 제보 → 즉시 경고 반영
        res = client.post("/report/accessibility",
                          json={"lat": 37.3900, "lng": 126.9505, "reason": "curb"})
        assert res.status_code == 200
        rid = res.json()["report_id"]

        res = client.get("/report/accessibility", params={"status": "new"})
        assert res.json()["count"] == 1

        # 경로 요약에 경고 노출
        res = client.post("/route/plan", json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "profile": "wheelchair_manual"})
        warns = res.json()["routes"][0]["summary"]["warnings"]
        assert any("이용자 제보" in w for w in warns)

        # 기각 → 경고 사라짐
        res = client.patch("/report/accessibility/%d" % rid,
                           json={"action": "reject", "note": "이상 없음"})
        assert res.status_code == 200
        res = client.post("/route/plan", json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "profile": "wheelchair_manual"})
        warns = res.json()["routes"][0]["summary"]["warnings"]
        assert not any("이용자 제보" in w for w in warns)

        # 삭제 → 목록에서 사라지고, 남은 오버라이드도 없다 (v1.17.0)
        res = client.post("/report/accessibility",
                          json={"lat": 37.3900, "lng": 126.9505, "reason": "blocked"})
        rid2 = res.json()["report_id"]
        res = client.post("/route/plan", json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "profile": "wheelchair_manual"})
        assert any("이용자 제보" in w for w in res.json()["routes"][0]["summary"]["warnings"])

        res = client.delete("/report/accessibility/%d" % rid2)
        assert res.status_code == 200 and res.json()["deleted"] is True
        assert all(r["report_id"] != rid2
                   for r in client.get("/report/accessibility").json()["items"])
        res = client.post("/route/plan", json={
            "origin": {"lat": 37.3900, "lng": 126.9500},
            "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
            "profile": "wheelchair_manual"})
        assert not any("이용자 제보" in w
                       for w in res.json()["routes"][0]["summary"]["warnings"])

        # 없는 제보 삭제는 404
        assert client.delete("/report/accessibility/99999").status_code == 404

        # 트랙 업로드
        res = client.post("/track/log", json={
            "route_id": "r_demo", "points": [
                {"seq": 0, "lat": 37.39, "lng": 126.95},
                {"seq": 1, "lat": 37.3901, "lng": 126.9502}],
            "meta": {"planned_dist_m": 200,
                     "geometry": [[37.39, 126.95], [37.3909, 126.9511]],
                     "outcome": "arrived"}})
        assert res.status_code == 200
        assert res.json()["stored_points"] == 2


# ───────────── CollectStore (db backend — 가짜 엔진) ─────────────
# 실제 DB 없이 트랜잭션 경계와 SQL 실행 형태만 본다. 엔진의 begin() 이 열릴 때마다 트랜잭션 1개로 센다.

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConn:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        eng = self.engine
        if eng.fail_on and eng.fail_on in sql:
            raise RuntimeError("DB 오류(시험)")
        eng.current.append((sql, params))
        if "RETURNING report_id" in sql:
            return _FakeResult([{"report_id": 7}])
        if "RETURNING override_id" in sql:
            return _FakeResult([{"override_id": 3}, {"override_id": 4}] if sql.startswith("DELETE")
                               else [{"override_id": 3}])
        if "RETURNING m.point_cnt" in sql:
            return _FakeResult([{"point_cnt": 3}])
        if sql.startswith("SELECT report_id"):
            return _FakeResult([{"report_id": 7, "lat": 37.39, "lon": 126.95, "reason": "curb", "status": "new"}])
        return _FakeResult([])


class _FakeEngine:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.committed = []       # 커밋된 트랜잭션마다 [(sql, params), ...]
        self.rolled_back = 0
        self.current = None

    def begin(self):
        import contextlib

        @contextlib.contextmanager
        def tx():
            self.current = []
            try:
                yield _FakeConn(self)
            except BaseException:
                self.rolled_back += 1
                raise
            else:
                self.committed.append(self.current)
        return tx()


def _db_store(fail_on=None):
    cs = CollectStore(dsn="postgresql+psycopg2://x/y")
    cs._engine = _FakeEngine(fail_on)
    return cs, cs._engine


def _writes(engine):
    """읽기 전용 트랜잭션(SELECT 하나)을 뺀 쓰기 트랜잭션 목록."""
    return [tx for tx in engine.committed if not all(sql.startswith("SELECT") for sql, _p in tx)]


def test_db_log_track_single_transaction_and_executemany():
    cs, eng = _db_store()
    pts = [{"seq": i, "lat": 37.39 + i * 1e-4, "lng": 126.95, "ts": None, "acc": 5.0} for i in range(3)]
    assert cs.log_track("r_demo", pts, {"outcome": "arrived"}) == 3
    assert len(eng.committed) == 1, "메타·점·점 수 갱신이 한 트랜잭션"
    stmts = eng.committed[0]
    inserts = [(sql, p) for sql, p in stmts if sql.startswith("INSERT INTO mv_route_track (")]
    assert len(inserts) == 1, "점마다 INSERT 하지 않는다"
    assert isinstance(inserts[0][1], list) and [p["seq"] for p in inserts[0][1]] == [0, 1, 2]
    assert inserts[0][1][0]["lon"] == 126.95
    assert len(stmts) == 3


def test_db_log_track_without_points_skips_point_insert():
    cs, eng = _db_store()
    cs.log_track("r_demo", [], {"outcome": "canceled"})
    assert len(eng.committed) == 1
    assert not [sql for sql, _p in eng.committed[0] if sql.startswith("INSERT INTO mv_route_track (")]


def test_db_add_report_and_warning_override_share_transaction():
    cs, eng = _db_store()
    out = cs.add_report(37.39, 126.9505, "curb", None, None, None, None)
    assert out == {"report_id": 7, "override_id": 3}
    assert len(eng.committed) == 1 and len(eng.committed[0]) == 2
    assert eng.committed[0][1][1]["rid"] == 7 and "미확인" in eng.committed[0][1][1]["value"]


def test_db_add_report_rolls_back_when_override_fails():
    cs, eng = _db_store(fail_on="INSERT INTO mv_access_override")
    with pytest.raises(RuntimeError):
        cs.add_report(37.39, 126.9505, "curb", None, None, None, None)
    assert eng.committed == [] and eng.rolled_back == 1, "제보만 남는 반쪽 상태가 없어야 한다"


@pytest.mark.parametrize("action,kwargs,n_stmts", [
    ("confirm", {}, 3), ("reject", {}, 2), ("apply", {"attr": "curb_cut", "value": "false"}, 3)])
def test_db_review_report_is_one_write_transaction(action, kwargs, n_stmts):
    cs, eng = _db_store()
    assert cs.review_report(7, action, note="확인", **kwargs) == {"report_id": 7, "action": action}
    writes = _writes(eng)
    assert len(writes) == 1 and len(writes[0]) == n_stmts
    assert writes[0][0][0].startswith("UPDATE mv_access_override SET status = 'retired'")
    assert writes[0][-1][0].startswith("UPDATE mv_access_report")


def test_db_review_report_rolls_back_when_status_update_fails():
    cs, eng = _db_store(fail_on="UPDATE mv_access_report")
    with pytest.raises(RuntimeError):
        cs.review_report(7, "confirm")
    assert _writes(eng) == [] and eng.rolled_back == 1, "철회·추가만 반영된 반쪽 상태가 없어야 한다"


def test_db_review_report_validation_errors_do_not_write():
    cs, eng = _db_store()
    with pytest.raises(ValueError):
        cs.review_report(7, "apply", attr="warning", value="x")
    with pytest.raises(ValueError):
        cs.review_report(7, "apply", attr="curb_cut", value=None)
    with pytest.raises(ValueError):
        cs.review_report(7, "bogus")
    assert _writes(eng) == []


def test_db_delete_report_is_one_write_transaction():
    cs, eng = _db_store()
    out = cs.delete_report(7)
    assert out == {"report_id": 7, "deleted": True, "deleted_overrides": 2}
    writes = _writes(eng)
    assert len(writes) == 1 and [sql.split(" WHERE")[0] for sql, _p in writes[0]] == [
        "DELETE FROM mv_access_override", "DELETE FROM mv_access_report"]
    cs2, eng2 = _db_store(fail_on="DELETE FROM mv_access_report")
    with pytest.raises(RuntimeError):
        cs2.delete_report(7)
    assert _writes(eng2) == [] and eng2.rolled_back == 1


def test_db_engine_gets_connect_timeout(monkeypatch):
    import sqlalchemy
    from route_service.collect import store as cstore
    seen = {}

    def fake_create_engine(dsn, **kw):
        seen.update(kw, dsn=dsn)
        return _FakeEngine()

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    CollectStore(dsn="postgresql+psycopg2://x/y")._db()
    assert seen["connect_args"] == {"connect_timeout": cstore.DB_CONNECT_TIMEOUT_S}
    assert seen["pool_pre_ping"] is True
    # psycopg2 가 아닌 드라이버에는 libpq 전용 인자를 넘기지 않는다
    assert cstore._connect_args("sqlite:///x.db") == {}
    assert cstore._connect_args("postgresql://x/y") == {"connect_timeout": cstore.DB_CONNECT_TIMEOUT_S}


def test_photo_base64_data_url_prefix_and_strict_decode():
    import base64
    raw = bytes(range(256)) * 4
    b64 = base64.b64encode(raw).decode()
    for sent in (b64, "data:image/jpeg;base64," + b64, "data:image/png;charset=utf-8;base64," + b64,
                 "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))):
        cs = CollectStore()
        rid = cs.add_report(37.39, 126.9505, "curb", None, None, sent, "image/jpeg")["report_id"]
        assert cs.get_report_photo(rid) == (raw, "image/jpeg")
    for bad in ("이건 base64 가 아니다", "data:image/jpeg;base64,@@@@", "AAA"):
        cs = CollectStore()
        with pytest.raises(ValueError, match="base64"):
            cs.add_report(37.39, 126.9505, "curb", None, None, bad, "image/jpeg")
        assert cs.list_reports() == [], "잘못된 사진이면 제보도 남기지 않는다"
    cs = CollectStore()
    big = base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()
    with pytest.raises(ValueError, match="2MB"):
        cs.add_report(37.39, 126.9505, "curb", None, None, big, "image/jpeg")
