# -*- coding: utf-8 -*-
"""연결요소 캐시(`NetworkStore.reachable_nodes`) — 키 구성과 무효화.

  1) 캐시 키는 통행 가능 판정이 읽는 프로필 값 전부를 포함한다 — id 가 같아도 avoid·폭·턱낮춤·
     정제 링크 신뢰도 하한이 다르면 서로 다른 결과를 돌려준다
  2) 오버라이드 적용·철회로 링크 통행성이 바뀌면 캐시를 비운다
  3) 요청 제약의 avoid 에 든 임의 문자열은 키에 넣지 않고, 항목 수에는 상한이 있다
"""
from dataclasses import replace

from conftest import make_graph
from route_service.engine import profiles as prof
from route_service.engine.graph import NetworkStore
from route_service.engine.overrides import apply_overrides

WM = prof.PROFILES["wheelchair_manual"]


def _store(G=None):
    s = NetworkStore()
    s.load_graph_object(G or make_graph(), version="t", region="t")
    return s


def test_constraint_profile_does_not_pollute_default_profile_cache():
    """avoid 를 비운 요청(계단 허용)이 먼저 들어와도 기본 프로필 결과는 계단을 회피한 집합이어야 한다."""
    st = _store()
    lvl = 5.0                                    # N4–N3(9도)는 경사로 막힌다 → N4 는 계단으로만 닿는다
    relaxed = replace(WM, avoid=())
    assert st.reachable_nodes(relaxed, lvl) == {"N1", "N2", "N3", "N4"}
    assert st.reachable_nodes(WM, lvl) == {"N1", "N2", "N3"}, "id 가 같은 제약 프로필의 결과가 재사용됐다"
    # 반대 순서 — 기본 프로필이 먼저 캐시돼도 제약 프로필은 자기 결과를 받는다
    st2 = _store()
    assert st2.reachable_nodes(WM, lvl) == {"N1", "N2", "N3"}
    assert st2.reachable_nodes(relaxed, lvl) == {"N1", "N2", "N3", "N4"}


def test_cache_key_covers_every_field_edge_passable_reads():
    # N1–N4 는 계단(회피) — 남는 망은 N1–N2–N3–N4 사슬에 N3–N5 가지가 붙은 모양
    G = make_graph()
    G["N1"]["N2"].update(link_type="crossing", curb_cut=False)        # 턱낮춤 요구 시 막히는 링크
    G["N4"]["N3"].update(slope=1.0, topo_source="derived", confidence=0.5)   # 신뢰도 하한을 올리면 막히는 링크
    G.add_node("N5", lat=37.3909, lon=126.9520, node_type="intersection")
    G.add_edge("N3", "N5", length=80.0, slope=1.0, link_type="sidewalk", width=1.0,   # 폭 하한을 올리면 막히는 링크
               curb_cut=None, surface=None, link_name=None, geometry=None)
    st = _store(G)
    base = replace(WM, min_width_m=0.0, requires_curb_cut=False, derived_min_confidence=0.0)
    assert st.reachable_nodes(base, 8.0) == {"N1", "N2", "N3", "N4", "N5"}
    assert st.reachable_nodes(replace(base, requires_curb_cut=True), 8.0) == {"N2", "N3", "N4", "N5"}
    assert st.reachable_nodes(replace(base, min_width_m=1.1), 8.0) == {"N1", "N2", "N3", "N4"}
    assert st.reachable_nodes(replace(base, derived_min_confidence=0.9), 8.0) == {"N1", "N2", "N3", "N5"}
    # 판정 값이 같은 요청은 같은 캐시 항목을 쓴다(avoid 순서는 판정에 영향이 없다)
    a = st.reachable_nodes(replace(base, avoid=("steps", "overpass")), 8.0)
    b = st.reachable_nodes(replace(base, avoid=("overpass", "steps")), 8.0)
    assert a is b


def test_unknown_avoid_strings_do_not_add_cache_entries():
    """알려진 링크 유형이 아닌 avoid 문자열은 어떤 링크와도 일치하지 않는다 — 같은 캐시 항목을 쓴다."""
    st = _store()
    base = st.reachable_nodes(WM, 8.0)
    for i in range(50):
        got = st.reachable_nodes(replace(WM, avoid=tuple(WM.avoid) + ("x-%d" % i, "STEPS ")), 8.0)
        assert got is base
    assert len(st._components) == 1
    # 알려진 유형은 종전대로 구분된다
    assert st.reachable_nodes(replace(WM, avoid=("x-1",)), 5.0) == {"N1", "N2", "N3", "N4"}
    assert st.reachable_nodes(WM, 5.0) == {"N1", "N2", "N3"}


def test_component_cache_size_is_bounded_and_drops_oldest():
    """경사 상한을 요청마다 달리 줘도 항목 수가 상한을 넘지 않고, 먼저 들어온 것부터 버린다."""
    from route_service.engine import graph as g
    st = _store()
    first = st.reachable_nodes(WM, 1.0)
    for i in range(g.COMPONENT_CACHE_MAX + 20):
        st.reachable_nodes(WM, 2.0 + i * 0.01)
        assert len(st._components) <= g.COMPONENT_CACHE_MAX
    assert len(st._components) == g.COMPONENT_CACHE_MAX
    last_key = st._component_key(WM, 2.0 + (g.COMPONENT_CACHE_MAX + 19) * 0.01)
    assert last_key in st._components and st._component_key(WM, 1.0) not in st._components
    again = st.reachable_nodes(WM, 1.0)                  # 버려진 조합은 다시 계산해 같은 결과를 낸다
    assert again == first and again is not first


def test_override_apply_and_revert_invalidate_component_cache():
    st = _store()
    assert "N3" in st.reachable_nodes(WM, 8.0)           # 캐시 생성
    block = [{"lat": 37.39045, "lon": 126.9511, "radius_m": 20, "attr": "passable", "value": "false"}]
    stat = st.update_graph(lambda G: apply_overrides(G, block))   # N2–N3 통행 불가 승인
    assert stat["blocked"] == 1
    assert st.reachable_nodes(WM, 8.0) == {"N1", "N2"}, "통행 불가 적용 뒤에도 종전 연결요소가 남았다"
    st.update_graph(lambda G: apply_overrides(G, []))    # 철회
    assert st.reachable_nodes(WM, 8.0) == {"N1", "N2", "N3"}


def test_update_graph_clears_cache_even_when_update_fails():
    st = _store()
    first = st.reachable_nodes(WM, 8.0)

    def boom(G):
        G["N2"]["N3"]["blocked"] = True                  # 일부만 적용된 뒤 실패
        raise ValueError("x")
    try:
        st.update_graph(boom)
    except ValueError:
        pass
    assert st.reachable_nodes(WM, 8.0) is not first
    assert st.reachable_nodes(WM, 8.0) == {"N1", "N2"}
    st.invalidate_components()
    assert st._components == {}


def test_api_passable_override_refreshes_snap_candidates(tmp_path, monkeypatch):
    """통행 불가 승인(PATCH apply) 뒤 `_apply_overrides_safe` 가 캐시를 비운다."""
    import importlib
    import pickle
    from fastapi.testclient import TestClient

    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)
    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("POI_BACKEND", "none")
    monkeypatch.setenv("POI_DB_DSN", "")
    monkeypatch.setenv("ROUTE_API_TOKEN", "")
    import route_service.config as cfg
    importlib.reload(cfg)
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    with TestClient(m.app) as c:
        assert "N3" in m.NET.reachable_nodes(WM, 12.0)
        rid = c.post("/report/accessibility",
                     json={"lat": 37.39045, "lng": 126.9511, "reason": "blocked"}).json()["report_id"]
        r = c.patch("/report/accessibility/%d" % rid,
                    json={"action": "apply", "attr": "passable", "value": "false"})
        assert r.status_code == 200, r.text
        assert r.json()["overrides"]["blocked"] == 1
        assert m.NET.reachable_nodes(WM, 12.0) == {"N1", "N2"}
        assert c.delete("/report/accessibility/%d" % rid).status_code == 200
        assert "N3" in m.NET.reachable_nodes(WM, 12.0)
