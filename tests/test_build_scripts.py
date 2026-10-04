# -*- coding: utf-8 -*-
"""그래프 빌드·정제 스크립트의 저장 안전성·재실행 안전성·빈 입력 처리 — 합성 그래프로 검증한다.

실데이터(도엽·DEM·운영 그래프)는 쓰지 않는다. 좌표 변환 라이브러리(pyproj)가 없는 환경에서도 돌도록
스크립트 main 을 부를 때는 평면 근사 변환기를 끼워 넣는다(_fake_pyproj).
"""
from __future__ import annotations

import csv
import json
import math
import os
import pickle
import sys
import types

import networkx as nx
import pytest

from route_service.topomap import graphio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAT, LON = 37.3900, 126.9500
KX, KY = 111_320.0 * math.cos(math.radians(LAT)), 110_540.0


def _ll(dx, dy):
    """원점에서 동쪽 dx m, 북쪽 dy m 인 좌표 속성."""
    return {"lat": LAT + dy / KY, "lon": LON + dx / KX}


@pytest.fixture
def _fake_pyproj(monkeypatch):
    """pyproj.Transformer 대역 — 원점 기준 평면 m 좌표로 바꾸는 근사 변환(시험 범위 수백 m 에서 충분)."""
    class _T:
        def __init__(self, src):
            self.to_plane = str(src).endswith("4326")

        def transform(self, x, y):
            if self.to_plane:
                return ((x - LON) * KX, (y - LAT) * KY)
            return (LON + x / KX, LAT + y / KY)

    class Transformer:
        @staticmethod
        def from_crs(src, dst, always_xy=True):
            return _T(src)

    mod = types.ModuleType("pyproj")
    mod.Transformer = Transformer
    monkeypatch.setitem(sys.modules, "pyproj", mod)
    return mod


def _dump(G, path):
    with open(path, "wb") as f:
        pickle.dump(G, f)
    return str(path)


def _load(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _run(monkeypatch, module, argv):
    """스크립트 main 을 argv 로 실행한다. 종료 코드(정상 종료면 0)를 돌려준다."""
    monkeypatch.setattr(sys, "argv", [module.__name__] + [str(a) for a in argv])
    try:
        module.main()
    except SystemExit as e:
        return e.code if e.code is not None else 0
    return 0


# ───────────── graphio: 원자적 저장 · 입력 덮어쓰기 거부 ─────────────

def _tiny():
    G = nx.Graph()
    G.add_node(1, **_ll(0, 0))
    G.add_node(2, **_ll(10, 0))
    G.add_edge(1, 2, length=10.0, link_type="sidewalk")
    return G


def test_save_graph_roundtrip_same_pickle_format(tmp_path):
    G = _tiny()
    out = tmp_path / "out.gpickle"
    assert graphio.save_graph(G, str(out)) == str(out)
    H = _load(out)
    assert sorted(H.nodes) == [1, 2] and H.edges[1, 2]["length"] == 10.0
    assert out.read_bytes() == pickle.dumps(G), "저장 내용은 pickle.dump(G, f) 와 같아야 한다"
    assert os.listdir(tmp_path) == ["out.gpickle"], "임시 파일이 남지 않는다"


def test_save_graph_failure_keeps_previous_output(tmp_path):
    """쓰는 도중 실패하면 기존 출력 파일은 그대로이고 임시 파일도 남지 않는다."""
    out = tmp_path / "out.gpickle"
    out.write_bytes(b"previous")

    class Unpicklable:
        def __reduce__(self):
            raise RuntimeError("직렬화 실패(시험)")

    G = _tiny()
    G.graph["bad"] = Unpicklable()
    with pytest.raises(RuntimeError):
        graphio.save_graph(G, str(out))
    assert out.read_bytes() == b"previous"
    assert os.listdir(tmp_path) == ["out.gpickle"]


def test_save_graph_refuses_to_overwrite_input(tmp_path):
    src = _dump(_tiny(), tmp_path / "in.gpickle")
    before = open(src, "rb").read()
    G = _tiny()
    G.add_node(3, **_ll(20, 0))
    with pytest.raises(graphio.GraphIOError, match="--overwrite-input"):
        graphio.save_graph(G, src, input_path=src)
    # 같은 파일을 다른 표기로 가리켜도 거부한다
    alias = os.path.join(str(tmp_path), ".", "in.gpickle")
    with pytest.raises(graphio.GraphIOError):
        graphio.save_graph(G, alias, input_path=src)
    assert open(src, "rb").read() == before and os.listdir(tmp_path) == ["in.gpickle"]


def test_save_graph_overwrite_input_leaves_backup(tmp_path):
    src = _dump(_tiny(), tmp_path / "in.gpickle")
    before = open(src, "rb").read()
    G = _tiny()
    G.add_node(3, **_ll(20, 0))
    graphio.save_graph(G, src, input_path=src, overwrite_input=True)
    assert 3 in _load(src)
    assert open(src + ".bak", "rb").read() == before
    assert sorted(os.listdir(tmp_path)) == ["in.gpickle", "in.gpickle.bak"]


def test_check_output_path_allows_distinct_or_missing(tmp_path):
    src = _dump(_tiny(), tmp_path / "in.gpickle")
    graphio.check_output_path(str(tmp_path / "out.gpickle"), src)
    graphio.check_output_path(None, src)          # 보고서만 내는 실행(--out 없음)
    graphio.check_output_path(src, None)
    assert not graphio.same_path(None, src)


@pytest.mark.parametrize("script", [
    "apply_city_crosswalks", "attach_crosswalk_points", "apply_manual_links", "restitch_boundary",
    "refine_detour_links", "mark_stairs_from_topomap", "build_corridor_hybrid", "enrich_osm_with_topomap"])
def test_graph_writing_scripts_use_shared_saver(script):
    """그래프를 저장하는 스크립트 8개는 공용 저장 함수를 쓰고, 직접 pickle.dump 하지 않는다."""
    with open(os.path.join(ROOT, "scripts", script + ".py"), encoding="utf-8") as f:
        src = f.read()
    assert "graphio.save_graph(" in src and "pickle.dump(" not in src
    assert "--overwrite-input" in src and "graphio.check_output_path(" in src


def test_script_refuses_out_equal_to_graph(tmp_path, monkeypatch, capsys):
    """--out 이 --graph 와 같으면 계산 전에 멈추고(종료 코드 2) 입력은 바뀌지 않는다."""
    from scripts import apply_manual_links as aml
    from scripts import restitch_boundary as rb
    src = _dump(_tiny(), tmp_path / "in.gpickle")
    before = open(src, "rb").read()
    links = tmp_path / "links.json"
    links.write_text(json.dumps({"links": [
        {"id": "X-1", "from": _ll(0, 30), "to": {"node": 1}}]}), encoding="utf-8")
    assert _run(monkeypatch, aml, ["--graph", src, "--links", links, "--out", src]) == 2
    assert _run(monkeypatch, rb, ["--graph", src, "--out", src,
                                  "--report", tmp_path / "r.csv"]) == 2
    assert "입력 그래프와 같습니다" in capsys.readouterr().err
    assert open(src, "rb").read() == before
    # 명시적 옵션이 있으면 .bak 을 남기고 진행한다
    assert _run(monkeypatch, aml, ["--graph", src, "--links", links, "--out", src, "--overwrite-input"]) == 0
    assert open(src + ".bak", "rb").read() == before and "MX-1_a" in _load(src)


# ───────────── 계단 재분류: 감사와 반영이 같은 기준 ─────────────

shapely = pytest.importorskip("shapely")


def _stairs_fixture():
    """링크 4개와 계단 면형 3개.
      long  : 200m 보도가 계단 모서리를 4m 스친다            → 대상 아님(관통비 2%)
      short : 10m 보도가 계단 위를 통째로 지난다             → 대상
      curvy : 직선 10m 인데 기재 길이 30m(실제는 굽은 길)    → 대상 아님(직선성 가드)
      steps : 이미 steps                                    → 건드리지 않음
    """
    from shapely.geometry import Polygon
    from route_service.topomap.obstacles import STAIRS, ObstacleIndex

    def box(x0, x1, y0, y1):
        return Polygon([(LON + x0 / KX, LAT + y0 / KY), (LON + x1 / KX, LAT + y0 / KY),
                        (LON + x1 / KX, LAT + y1 / KY), (LON + x0 / KX, LAT + y1 / KY)])

    G = nx.Graph()
    pts = {"L1": (0, 0), "L2": (200, 0), "S1": (0, 100), "S2": (10, 100),
           "C1": (0, 200), "C2": (10, 200), "T1": (0, 300), "T2": (10, 300)}
    for n, (dx, dy) in pts.items():
        G.add_node(n, node_type="unknown", **_ll(dx, dy))
    e = dict(slope=0.0, width=2.0, curb_cut=None, surface=None, link_name=None, geometry=None)
    G.add_edge("L1", "L2", length=200.0, link_type="sidewalk", **e)
    G.add_edge("S1", "S2", length=10.0, link_type="sidewalk", **e)
    G.add_edge("C1", "C2", length=30.0, link_type="sidewalk", **e)
    G.add_edge("T1", "T2", length=10.0, link_type="steps", **e)
    idx = ObstacleIndex([{"obstacle": STAIRS, "geom": box(0, 4, -3, 3)},
                         {"obstacle": STAIRS, "geom": box(-1, 11, 97, 103)},
                         {"obstacle": STAIRS, "geom": box(-1, 11, 197, 203)},
                         {"obstacle": STAIRS, "geom": box(-1, 11, 297, 303)}])
    return G, idx


def test_should_reclassify_applies_length_and_both_guards():
    from scripts import mark_stairs_from_topomap as ms
    assert ms.should_reclassify(10.0, 10.0, 10.0, 3.0)
    assert not ms.should_reclassify(2.9, 5.0, 5.0, 3.0), "관통 길이 미달"
    assert not ms.should_reclassify(4.0, 200.0, 200.0, 3.0), "긴 링크가 모서리를 스친 것"
    assert not ms.should_reclassify(10.0, 10.0, 30.0, 3.0), "기재 길이가 직선보다 훨씬 길다"
    assert ms.should_reclassify(10.0, 10.0, 0.0, 3.0), "기재 길이가 없으면 직선성 가드는 건너뛴다"
    assert not ms.should_reclassify(10.0, 0.0, 10.0, 3.0)


def test_apply_graph_uses_same_guards_as_audit(tmp_path):
    """긴 보도가 계단 모서리를 스친 것만으로 steps 가 되지 않고, 반영 건수가 감사 건수와 같다."""
    from scripts import mark_stairs_from_topomap as ms
    G, idx = _stairs_fixture()
    src, out = _dump(G, tmp_path / "in.gpickle"), str(tmp_path / "out.gpickle")
    changed = ms.apply_graph(idx, src, out, 3.0)
    assert [(u, v) for u, v, _o, _m in changed] == [("S1", "S2")]
    H = _load(out)
    assert H.edges["L1", "L2"]["link_type"] == "sidewalk", "200m 보도 전체가 계단이 되면 안 된다"
    assert H.edges["C1", "C2"]["link_type"] == "sidewalk"
    assert H.edges["S1", "S2"]["link_type"] == "steps" and H.edges["S1", "S2"]["_orig_link_type"] == "sidewalk"
    assert H.edges["S1", "S2"]["attr_source"] == "topo1k:C0390000"
    assert "_orig_link_type" not in H.edges["T1", "T2"]
    assert _load(src).edges["S1", "S2"]["link_type"] == "sidewalk", "입력 파일은 그대로"

    # 같은 그래프를 표로 내보내 감사하면 '재분류 대상' 건수가 반영 건수와 같다
    nodes_csv, links_csv = tmp_path / "n.csv", tmp_path / "l.csv"
    with open(nodes_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["node_id", "lat", "lon"])
        for n, a in G.nodes(data=True):
            w.writerow([n, a["lat"], a["lon"]])
    with open(links_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["f_node", "t_node", "length_m", "link_type"])
        for u, v, d in G.edges(data=True):
            w.writerow([u, v, d["length"], d["link_type"]])
    rows = ms.audit_csv(idx, str(links_csv), str(nodes_csv), 3.0)
    marked = [r for r in rows if r[0]["link_type"] not in ms.ALREADY_AVOIDED and r[4]]
    assert len(rows) == 4 and len(marked) == len(changed) == 1
    assert {(r[0]["f_node"], r[0]["t_node"]) for r in marked} == {("S1", "S2")}


def test_edge_with_geometry_is_judged_on_its_own_shape():
    """geometry 가 있는 링크는 그 선형의 길이를 기준으로 가드를 건다(굽은 선형이 직선성 가드에 걸리지 않는다)."""
    from scripts import mark_stairs_from_topomap as ms
    G, idx = _stairs_fixture()
    def ll(dx, dy):
        p = _ll(dx, dy)
        return (p["lat"], p["lon"])

    # C1–C2 를 실제로 굽은 선형(북쪽으로 10m 돌아가는 길, 길이 30m)으로 준다 — 계단 면형(y 197~203)은
    # 양 끝 3m 씩만 지난다. 관통 6m / 선형 30m = 20% < 30% → 대상 아님
    G.edges["C1", "C2"]["geometry"] = [ll(0, 200), ll(0, 210), ll(10, 210), ll(10, 200)]
    ok, m = ms.edge_stairs_verdict(idx, G, "C1", "C2", 3.0)
    assert not ok and 5.0 < m < 7.0
    # 선형 전체가 계단 위에 있으면 대상 — 판정선 길이(약 10.2m)와 기재 길이가 맞아 직선성 가드를 통과한다
    G.edges["S1", "S2"]["geometry"] = [ll(0, 100), ll(5, 101), ll(10, 100)]
    G.edges["S1", "S2"]["length"] = 10.2
    ok, m = ms.edge_stairs_verdict(idx, G, "S1", "S2", 3.0)
    assert ok and m > 9.5
    # 굽은 선형 대부분이 계단 위에 있으면(기재 길이 = 선형 길이) 직선거리가 짧아도 대상이다
    G.add_node("Z1", **_ll(0, 400)); G.add_node("Z2", **_ll(2, 400))
    G.add_edge("Z1", "Z2", length=22.0, link_type="sidewalk",
               geometry=[ll(0, 400), ll(0, 410), ll(2, 410), ll(2, 400)])
    from shapely.geometry import Polygon
    from route_service.topomap.obstacles import STAIRS, ObstacleIndex
    box = Polygon([(LON - 1 / KX, LAT + 399 / KY), (LON + 3 / KX, LAT + 399 / KY),
                   (LON + 3 / KX, LAT + 411 / KY), (LON - 1 / KX, LAT + 411 / KY)])
    idx2 = ObstacleIndex([{"obstacle": STAIRS, "geom": box}])
    assert ms.edge_stairs_verdict(idx2, G, "Z1", "Z2", 3.0)[0]


# ───────────── 횡단보도 반영: 재실행 안전 · 빈 입력 ─────────────

def _crosswalk_graph():
    """남북 도로(x=0) 양쪽에 보도(x=−8, x=+8). 도로 한가운데 횡단보도 점이 오면 양쪽 보도를 잇는다."""
    G = nx.Graph()
    pts = {"R1": (0, -50), "R2": (0, 50), "W1": (-8, -40), "W2": (-8, 40), "E1": (8, -40), "E2": (8, 40)}
    for n, (dx, dy) in pts.items():
        G.add_node(n, node_type="unknown", **_ll(dx, dy))
    e = dict(slope=0.0, width=None, curb_cut=None, tactile_paving=None, surface=None, link_name=None, geometry=None)
    G.add_edge("R1", "R2", length=100.0, link_type="road", **e)
    G.add_edge("W1", "W2", length=80.0, link_type="sidewalk", **e)
    G.add_edge("E1", "E2", length=80.0, link_type="sidewalk", **e)
    return G


def _cw_feature(no, dx, dy, length=16.0):
    p = _ll(dx, dy)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
            "properties": {"mgmt_no": no, "cw_length_m": length, "cw_width_m": 4.0}}


def _write_cw(path, feats):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    return str(path)


def test_cwx_id_helpers():
    from scripts import apply_city_crosswalks as ac
    G = _crosswalk_graph()
    assert ac.applied_traces(G) == {"cwx_nodes": 0, "attached_nodes": 0}
    assert ac.cwx_start_index(G) == 0 and ac.next_cwx_id(G, 0) == ("cwx000001", 1)
    G.add_node("cwx000007", **_ll(1, 1))
    G.add_node("cwx_misc", **_ll(2, 2))
    G.nodes["E2"]["cw_mgmt_nos"] = ["A-1"]
    assert ac.applied_traces(G) == {"cwx_nodes": 2, "attached_nodes": 1}
    assert ac.cwx_start_index(G) == 7 and ac.next_cwx_id(G, 7) == ("cwx000008", 8)
    G.add_node("cwx000008", **_ll(3, 3))
    assert ac.next_cwx_id(G, 7) == ("cwx000009", 9), "이미 있는 ID 는 건너뛴다"


def test_apply_city_crosswalks_refuses_second_run(tmp_path, monkeypatch, _fake_pyproj, capsys):
    pytest.importorskip("scipy")
    from scripts import apply_city_crosswalks as ac
    src = _dump(_crosswalk_graph(), tmp_path / "base.gpickle")
    cw = _write_cw(tmp_path / "cw.geojson", [_cw_feature("A-1", 0, 0), _cw_feature("A-2", 8, 45, None)])
    out1, out2 = str(tmp_path / "cw1.gpickle"), str(tmp_path / "cw2.gpickle")

    assert _run(monkeypatch, ac, ["--graph", src, "--crosswalks", cw, "--out", out1]) == 0
    G1 = _load(out1)
    assert {"cwx000001", "cwx000002"} <= set(G1.nodes), "깨끗한 그래프에서는 번호가 1부터(종전과 같다)"
    assert G1.has_edge("cwx000001", "cwx000002") and G1.edges["cwx000001", "cwx000002"]["link_type"] == "crossing"
    assert G1.nodes["E2"]["crosswalk_cnt"] == 1 and G1.nodes["E2"]["cw_mgmt_nos"] == ["A-2"]
    pos1 = {n: (G1.nodes[n]["lat"], G1.nodes[n]["lon"]) for n in ("cwx000001", "cwx000002")}

    # 이미 반영된 그래프에 다시 돌리면 멈춘다 — 출력도 만들지 않는다
    code = _run(monkeypatch, ac, ["--graph", out1, "--crosswalks", cw, "--out", out2])
    assert isinstance(code, str) and "이미 반영" in code and "--force" in code
    assert not os.path.exists(out2)

    # --force: 새 분할 노드는 기존 번호 뒤에서 이어 매기고 기존 cwx 노드 좌표는 그대로다
    cw3 = _write_cw(tmp_path / "cw3.geojson",
                    [_cw_feature("A-1", 0, 0), _cw_feature("A-2", 8, 45, None), _cw_feature("A-3", 0, 25)])
    capsys.readouterr()
    assert _run(monkeypatch, ac, ["--graph", out1, "--crosswalks", cw3, "--out", out2, "--force"]) == 0
    assert "경고" in capsys.readouterr().out
    G2 = _load(out2)
    assert {n: (G2.nodes[n]["lat"], G2.nodes[n]["lon"]) for n in pos1} == pos1, "기존 분할 노드를 덮어쓰지 않는다"
    new = sorted(n for n in G2.nodes if str(n).startswith("cwx") and n not in pos1)
    assert new == ["cwx000003", "cwx000004"]
    assert G2.has_edge("cwx000003", "cwx000004")
    assert abs(G2.nodes["cwx000003"]["lat"] - _ll(0, 25)["lat"]) < 1e-6


def test_apply_city_crosswalks_empty_inputs_exit_with_message(tmp_path, monkeypatch, _fake_pyproj):
    pytest.importorskip("scipy")
    from scripts import apply_city_crosswalks as ac
    src = _dump(_crosswalk_graph(), tmp_path / "base.gpickle")
    out = str(tmp_path / "out.gpickle")
    empty = _write_cw(tmp_path / "empty.geojson", [])
    code = _run(monkeypatch, ac, ["--graph", src, "--crosswalks", empty, "--out", out])
    assert isinstance(code, str) and "0건" in code and "empty.geojson" in code
    G0 = nx.Graph()
    G0.add_node("N", **_ll(0, 0))
    no_edges = _dump(G0, tmp_path / "noedge.gpickle")
    cw = _write_cw(tmp_path / "cw.geojson", [_cw_feature("A-1", 0, 0)])
    code = _run(monkeypatch, ac, ["--graph", no_edges, "--crosswalks", cw, "--out", out])
    assert isinstance(code, str) and "링크가 없습니다" in code
    assert not os.path.exists(out)


def _pednet_csv(tmp_path, with_width=True):
    node, link = tmp_path / "pn.csv", tmp_path / "pl.csv"
    with open(node, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["NODE_ID", "X", "Y", "NODE_TYPE"])
        for i, dx in enumerate((0, 100, 200, 300), 1):
            p = _ll(dx, 5)
            w.writerow(["P%d" % i, p["lon"], p["lat"], "sidewalk"])
    with open(link, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["F_NODE", "T_NODE", "LENGTH", "LINK_TYPE", "WIDTH", "SURFACE", "LINK_NAME"])
        for i in (1, 2, 3):
            w.writerow(["P%d" % i, "P%d" % (i + 1), 100.0, "sidewalk", "2.5" if with_width else "", "", ""])
    return str(node), str(link)


def test_fill_crosswalk_widths_empty_inputs(tmp_path, monkeypatch, _fake_pyproj, capsys):
    pytest.importorskip("scipy")
    from scripts import fill_crosswalk_widths as fw
    node, link = _pednet_csv(tmp_path)
    out = str(tmp_path / "w.csv")
    empty = _write_cw(tmp_path / "empty.geojson", [])
    code = _run(monkeypatch, fw, ["--crosswalks", empty, "--pednet-node", node, "--pednet-link", link, "--out", out])
    assert isinstance(code, str) and "횡단보도가 0건" in code

    cw = _write_cw(tmp_path / "cw.geojson", [_cw_feature("A-1", 50, 0)])
    (tmp_path / "nw").mkdir()
    node2, link2 = _pednet_csv(tmp_path / "nw", with_width=False)
    code = _run(monkeypatch, fw, ["--crosswalks", cw, "--pednet-node", node2, "--pednet-link", link2, "--out", out])
    assert isinstance(code, str) and "보도 링크가 0건" in code
    assert not os.path.exists(out), "빈 입력이면 산출 파일을 만들지 않는다"

    # 반경 안에 보도가 하나도 없어 채움이 0건 — 통계 출력에서 죽지 않는다
    far = _write_cw(tmp_path / "far.geojson", [_cw_feature("A-9", 50, 500)])
    assert _run(monkeypatch, fw, ["--crosswalks", far, "--pednet-node", node, "--pednet-link", link, "--out", out]) == 0
    assert "채움 0/1" in capsys.readouterr().out
    # 정상 입력은 종전대로 채운다
    assert _run(monkeypatch, fw, ["--crosswalks", cw, "--pednet-node", node, "--pednet-link", link, "--out", out]) == 0
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["approach_width_m"] == "2.5" and rows[0]["approach_width_dist_m"] == "5.0"


# ───────────── 정제 보고서 경로 ─────────────

def test_gaps_report_path_never_equals_report_path():
    from scripts import refine_detour_links as rd
    assert rd.gaps_report_path("data/refine_report.csv") == "data/refine_report_gaps.csv"
    for p in ("data/refine_report.txt", "data/refine_report", "data/run.csv.d/report.tsv", "r.CSV"):
        assert rd.gaps_report_path(p) != p
    assert rd.gaps_report_path("data/refine_report.txt") == "data/refine_report_gaps.txt"
    assert rd.gaps_report_path("data/refine_report") == "data/refine_report_gaps"
    assert rd.gaps_report_path("data/run.csv.d/report.tsv") == "data/run.csv.d/report_gaps.tsv"


# ───────────── 하이브리드 빌드: DEM 결측 집계 · leg 오류 ─────────────

def test_dem_sampler_counts_links_without_elevation():
    """표고를 얻지 못해도 경사 0.0 으로 진행하는 동작은 그대로이고, 그런 링크 수를 센다."""
    import numpy as np
    from scripts import build_corridor_hybrid as bh

    class FakeDS:
        def index(self, x, y):
            if x < 0:
                raise ValueError("범위 계산 실패(시험)")
            return int(y), int(x)

    s = bh._DemSampler(None)
    assert s.requested is False and s.ds is None
    assert s.slope_deg((0, 0), (1, 1), 10.0) == 0.0 and (s.links, s.missing) == (1, 1)

    s = bh._DemSampler(None)
    s.requested, s.ds, s.nodata = True, FakeDS(), -9999.0
    s.band = np.array([[10.0, 11.0, -9999.0], [10.0, 12.0, 13.0]])
    assert s.slope_deg((0, 0), (1, 0), 10.0) == round(abs(math.degrees(math.atan2(1.0, 10.0))), 2)
    assert (s.links, s.missing) == (1, 0)
    assert s.slope_deg((0, 0), (2, 0), 10.0) == 0.0      # nodata
    assert s.slope_deg((0, 0), (9, 9), 10.0) == 0.0      # 범위 밖
    assert s.slope_deg((-1, 0), (1, 0), 10.0) == 0.0     # 예외
    s.note_missing()                                      # 좌표 미상
    assert (s.links, s.missing) == (5, 4)
    assert s.slope_deg((0, 0), (1, 0), 0.0) == 0.0 and s.missing == 4, "길이 0 은 결측이 아니다"


def test_dem_missing_summary_threshold():
    from scripts import build_corridor_hybrid as bh
    assert 0 < bh.DEM_MISSING_MAX_RATIO < 0.5
    ok = bh.dem_missing_summary(True, True, 100, 5)
    assert ok["missing_ratio"] == 0.05 and ok["exceeded"] is False
    bad = bh.dem_missing_summary(True, True, 100, 6)
    assert bad["exceeded"] is True and bad["missing_links"] == 6 and bad["links"] == 100
    assert bh.dem_missing_summary(True, False, 40, 40)["exceeded"] is True, "DEM 을 지정했는데 열지 못한 경우"
    assert bh.dem_missing_summary(False, False, 40, 40)["exceeded"] is False, "--dem 없이 돌린 실행은 의도된 0.0"
    assert bh.dem_missing_summary(True, True, 0, 0)["exceeded"] is False


def test_dem_crs_warning_only_when_not_5186():
    from scripts import build_corridor_hybrid as bh

    class CRS:
        def __init__(self, epsg):
            self._e = epsg

        def to_epsg(self):
            return self._e

    assert bh.dem_crs_warning(CRS(5186)) is None
    assert bh.dem_crs_warning(None) is None and bh.dem_crs_warning(CRS(None)) is None
    assert "EPSG:4326" in bh.dem_crs_warning(CRS(4326)) and "5186" in bh.dem_crs_warning(CRS(5179))


def _hybrid_inputs(tmp_path):
    """동서 도로(노드 1~4, 100m 간격)와 그 북쪽 5m 의 보도망(P1~P4)."""
    G = nx.Graph()
    for i, dx in enumerate((0, 100, 200, 300), 1):
        G.add_node(i, node_type="unknown", **_ll(dx, 0))
    for i in (1, 2, 3):
        G.add_edge(i, i + 1, length=100.0, slope=0.0, link_type="road", width=3.0, curb_cut=True,
                   surface=None, link_name=None, geometry=None)
    graph = _dump(G, tmp_path / "base.gpickle")
    node, link = _pednet_csv(tmp_path)
    cw = _write_cw(tmp_path / "cw.geojson", [_cw_feature("A-1", 150, 2)])
    legs = tmp_path / "legs.json"
    a, b = _ll(0, 0), _ll(300, 0)
    legs.write_text(json.dumps([{"name": "1a", "from": [a["lat"], a["lon"]], "to": [b["lat"], b["lon"]]}]),
                    encoding="utf-8")
    return ["--graph", graph, "--pednet-node", node, "--pednet-link", link,
            "--crosswalks", cw, "--legs", str(legs)]


def test_hybrid_build_fails_loudly_when_dem_gives_no_elevation(tmp_path, monkeypatch, _fake_pyproj, capsys):
    pytest.importorskip("scipy")
    from scripts import build_corridor_hybrid as bh
    base = _hybrid_inputs(tmp_path)
    out, rep = str(tmp_path / "hy.gpickle"), str(tmp_path / "hy.json")
    not_a_raster = tmp_path / "dem.tif"
    not_a_raster.write_bytes(b"not a raster")
    # 래스터 라이브러리가 없거나 파일을 열 수 없는 상황을 고정한다(설치 여부와 무관하게 같은 결과)
    monkeypatch.setitem(sys.modules, "rasterio", None)

    code = _run(monkeypatch, bh, base + ["--dem", not_a_raster, "--out", out, "--report", rep])
    assert code == bh.EXIT_DEM_MISSING != 0
    assert not os.path.exists(out), "결측 초과면 그래프를 저장하지 않는다"
    stat = json.load(open(rep, encoding="utf-8"))
    assert stat["saved"] is False and stat["dem"]["exceeded"] is True
    assert stat["dem"]["links"] == stat["dem"]["missing_links"] > 0 and stat["dem"]["opened"] is False
    assert "DEM 표고 결측 100.0%" in capsys.readouterr().out

    # 우회 옵션 — 경고는 내되 저장하고 정상 종료
    assert _run(monkeypatch, bh, base + ["--dem", not_a_raster, "--out", out, "--report", rep,
                                         "--allow-missing-dem"]) == 0
    assert os.path.exists(out) and json.load(open(rep, encoding="utf-8"))["saved"] is True
    H = _load(out)
    assert all(d["slope"] == 0.0 for _u, _v, d in H.edges(data=True) if d.get("topo_source") == "topo1k")

    # --dem 을 주지 않은 실행은 종전대로 저장한다(경사 0.0 이 의도된 동작)
    out2 = str(tmp_path / "hy2.gpickle")
    assert _run(monkeypatch, bh, base + ["--out", out2, "--report", rep]) == 0
    stat = json.load(open(rep, encoding="utf-8"))
    assert os.path.exists(out2) and stat["dem"]["requested"] is False and stat["dem"]["exceeded"] is False
    assert pickle.dumps(sorted(_load(out2).edges(data="link_type"), key=str)) == \
        pickle.dumps(sorted(H.edges(data="link_type"), key=str)), "DEM 유무는 위상에 영향을 주지 않는다"


def test_route_corridors_error_names_the_leg():
    from scripts import build_corridor_hybrid as bh
    G = nx.Graph()
    for i, dx in enumerate((0, 100), 1):
        G.add_node(i, node_type="unknown", **_ll(dx, 0))
    G.add_edge(1, 2, length=100.0, slope=0.0, link_type="sidewalk", width=2.0, curb_cut=True,
               surface=None, link_name=None, geometry=None)
    a, b = _ll(0, 0), _ll(100, 0)
    plane = lambda lon, lat: ((lon - LON) * KX, (lat - LAT) * KY)      # noqa: E731
    good = {"name": "1a", "from": [a["lat"], a["lon"]], "to": [b["lat"], b["lon"]]}
    lines = bh._route_corridors(G, [good], plane)
    assert len(lines) == 1 and len(lines[0]) >= 2
    with pytest.raises(RuntimeError, match="leg '2b'"):
        bh._route_corridors(G, [good, {"name": "2b", "from": [a["lat"], a["lon"]]}], plane)
    with pytest.raises(RuntimeError, match="leg '#2'"):
        bh._route_corridors(G, [good, {"from": [a["lat"]], "to": [b["lat"], b["lon"]]}], plane)


# ───────────── 빌드 의존성 · 무시 규칙 ─────────────

def _req_names(name):
    out = {}
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                pkg = line.replace(">=", " ").replace("==", " ").split()[0].lower()
                out[pkg] = line
    return out


def test_build_requirements_cover_imported_packages():
    reqs = _req_names("requirements-build.txt")
    assert "pyshp" in reqs, "import shapefile (topomap/extract.py, scripts/enrich_osm_with_topomap.py)"
    assert "scikit-image" in reqs, "skimage (topomap/centerline.py)"
    # features_from_bbox 에 (서, 남, 동, 북) 튜플을 넘기는 호출은 osmnx 2.x 형식이다
    assert reqs["osmnx"].replace(" ", "") == "osmnx>=2.0"
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        assert "xdem" not in f.read(), "어디서도 import 하지 않는 패키지"


def test_gitignore_covers_derived_layers_and_tracks():
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
        lines = {ln.strip() for ln in f}
    assert {"data/topomap_layers/", "tracks*.json", "track_analysis*.json"} <= lines
