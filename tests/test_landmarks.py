# -*- coding: utf-8 -*-
"""길안내 중 주변 랜드마크 (#88)."""
import math

from route_service.poi import landmarks as lm

LAT0, LON0 = 37.4174, 126.918
KX = 111_320.0 * math.cos(math.radians(LAT0))
KY = 110_540.0


def _ll(dx, dy):
    return (LAT0 + dy / KY, LON0 + dx / KX)


# 동쪽으로 곧게 800m
GEOM = [list(_ll(x, 0)) for x in range(0, 801, 50)]


def _c(kind, name, dx, dy, tier=1):
    lat, lng = _ll(dx, dy)
    return {"kind": kind, "name": name, "lat": lat, "lng": lng, "tier": tier}


def test_side_offset_along():
    items = lm.along_route(GEOM, [_c("bus_stop", "만안구청", 200, 8), _c("facility", "안양상업고등학교", 400, -20)])
    a = {it["name"]: it for it in items}
    assert a["만안구청"]["side"] == "left" and abs(a["만안구청"]["along_m"] - 200) < 1
    assert a["안양상업고등학교"]["side"] == "right" and abs(a["안양상업고등학교"]["offset_m"] - 20) < 1


def test_far_and_on_line():
    items = lm.along_route(GEOM, [_c("facility", "먼건물", 300, 60), _c("bus_stop", "선위정류장", 300, 1)])
    assert [it["name"] for it in items] == ["선위정류장"]
    assert items[0]["side"] is None


def test_select_spacing_prefers_tier_then_offset():
    cands = [_c("facility", "퍼스트힐", 160, 10, tier=2), _c("bus_stop", "보건소·만안구청", 190, 12),
             _c("facility", "만안구보건소", 250, 5),
             _c("facility", "한림시티빌아파트", 420, 6, tier=2), _c("toilet", "명학공원 공중화장실", 700, 25)]
    items = lm.along_route(GEOM, cands)
    out = lm.select(items, 800)
    names = [o["name"] for o in out]
    assert names[0] == "보건소·만안구청"                 # 160~220m 창 안에서 1등급 + 가까운 것
    assert "만안구보건소" not in names                     # 앞 랜드마크에서 150m 이내
    assert "한림시티빌아파트" in names
    assert all(b["along_m"] - a["along_m"] >= lm.SPACING_M for a, b in zip(out, out[1:]))
    assert "명학공원 공중화장실" in names                  # 도착 30m 전(770m)보다 앞이면 들어온다
    out2 = lm.select(lm.along_route(GEOM, [_c("toilet", "명학공원 공중화장실", 790, 5)]), 800)
    assert out2 == []                                        # 도착 30m 이내는 도착 안내와 겹친다


def test_head_skip_and_dedup_same_name():
    cands = [_c("bus_stop", "명학역", 10, 5), _c("bus_stop", "성결대학교", 300, 15), _c("bus_stop", "성결대학교", 305, -6)]
    out = lm.select(lm.along_route(GEOM, cands), 800)
    assert [o["name"] for o in out] == ["성결대학교"] and out[0]["side"] == "right"


def test_walk_geometries_limit():
    walk = [GEOM[:5]]   # 0~200m 만 도보
    items = lm.along_route(GEOM, [_c("bus_stop", "A정류장", 100, 5), _c("bus_stop", "B정류장", 500, 5)], walk)
    assert [it["name"] for it in items] == ["A정류장"]


def test_usable_name():
    assert lm.usable_name("안양상업고등학교", "고등학교")
    for bad, t in [("일반음식점", "일반음식점"), ("안양동 491-2 공동주택", "아파트"), ("10동", "고등학교"),
                   ("C동", "유치원"), ("의원·치과의원·한의원·조산소·산후조리원", "의원·치과의원·한의원·조산소·산후조리원"),
                   ("연립주택", "연립주택"), ("공장", "공장")]:
        assert not lm.usable_name(bad, t), bad


def test_speech_josa_and_forms():
    s = lm.speech({"kind": "bus_stop", "name": "안양박물관·김중업건축박물관", "side": "right"})
    assert s == "오른쪽에 안양박물관, 김중업건축박물관 정류장이 있습니다"
    assert lm.speech({"kind": "facility", "name": "만안구보건소", "side": "left"}) == "왼쪽에 만안구보건소가 있습니다"
    assert lm.speech({"kind": "facility", "name": "안양상업고등학교", "side": None}) == "안양상업고등학교 근처를 지납니다"
    assert lm.speech({"kind": "facility", "name": "퍼스트힐", "side": "right"}) == "오른쪽에 퍼스트힐이 있습니다"
    assert lm.speech({"kind": "toilet", "name": "명학공원 공중화장실", "side": "left"}) == "왼쪽에 명학공원 공중화장실이 있습니다"
    assert lm.speech({"kind": "charger", "name": "장애인지원센터", "side": "right"}) == "오른쪽에 장애인지원센터 전동휠체어 충전기가 있습니다"
    assert lm.speech({"kind": "facility", "name": "M-tower", "side": "left"}).endswith("M-tower가 있습니다")


def test_for_route_non_db_backend_is_empty():
    class S:
        backend = "file"
    assert lm.for_route(S(), GEOM) == []


def test_for_route_uses_fetch(monkeypatch):
    class S:
        backend = "db"
    monkeypatch.setattr(lm, "fetch_candidates", lambda store, bbox: [_c("bus_stop", "만안구청", 300, 6)])
    out = lm.for_route(S(), GEOM)
    assert out and out[0]["speech"] == "왼쪽에 만안구청 정류장이 있습니다" and out[0]["along_m"] == 300


def test_for_route_swallows_errors(monkeypatch):
    class S:
        backend = "db"
    def boom(store, bbox):
        raise RuntimeError("db down")
    monkeypatch.setattr(lm, "fetch_candidates", boom)
    assert lm.for_route(S(), GEOM) == []
