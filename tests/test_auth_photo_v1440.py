# -*- coding: utf-8 -*-
"""인증 fail-closed · 관리 토큰 분리 · 제보 사진 형식 검증 (v1.44.0).

계약
  · 토큰이 설정되지 않은 서버는 보호 엔드포인트를 503 으로 거부한다(검사를 건너뛰지 않는다).
  · 인증 없이 열린 것은 /health · /meta/network · /profiles · API 문서뿐이다.
  · /admin/* 는 ROUTE_ADMIN_TOKEN 으로만 — 일반 토큰은 401.
  · 제보 사진은 JPEG·PNG·WebP 만. 형식은 매직 바이트로 판정하고 photo_mime 과 다르면 400.
  · 사진 응답은 바이트를 다시 판정해 Content-Type 을 정하고, 허용 형식이 아니면 내려받기로 돌린다.
"""
from __future__ import annotations

import base64
import importlib
import pickle
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_AUTH, API_AUTH, TEST_ADMIN_TOKEN, TEST_API_TOKEN, make_graph

CSP = "default-src 'none'; img-src 'self' data:; sandbox"


def _png_1x1() -> bytes:
    """실제로 열리는 1x1 PNG."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# JFIF 머리 + 끝 표지 — 형식 판정(매직 바이트)에 필요한 앞머리를 갖춘 JPEG 바이트
JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\x00" * 64 + b"\xff\xd9")
PNG = _png_1x1()
WEBP = b"RIFF" + struct.pack("<I", 26) + b"WEBPVP8 " + b"\x00" * 18


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _make(tmp_path, monkeypatch, api_token: str, admin_token: str):
    net = tmp_path / "network.gpickle"
    with open(net, "wb") as f:
        pickle.dump(make_graph(), f)
    monkeypatch.setenv("NETWORK_PATH", str(net))
    monkeypatch.setenv("NETWORK_VERSION", "test-auth")
    monkeypatch.setenv("POI_BACKEND", "none")
    monkeypatch.setenv("POI_DB_DSN", "")          # 수집 저장소 = 인메모리
    monkeypatch.setenv("ROUTE_API_TOKEN", api_token)
    monkeypatch.setenv("ROUTE_ADMIN_TOKEN", admin_token)
    import route_service.config as cfg
    importlib.reload(cfg)                          # Settings 기본값은 import 시점 env 로 고정된다
    cfg._settings = None
    import route_service.api.main as m
    importlib.reload(m)
    return m


PLAN = {"origin": {"lat": 37.3900, "lng": 126.9500},
        "destination": {"type": "coord", "lat": 37.3909, "lng": 126.9511},
        "profile": "wheelchair_manual"}


@pytest.fixture()
def no_token(tmp_path, monkeypatch):
    m = _make(tmp_path, monkeypatch, "", "")
    with TestClient(m.app) as c:
        yield c


@pytest.fixture()
def api_only(tmp_path, monkeypatch):
    m = _make(tmp_path, monkeypatch, TEST_API_TOKEN, "")
    with TestClient(m.app) as c:
        yield c


@pytest.fixture()
def full(tmp_path, monkeypatch):
    m = _make(tmp_path, monkeypatch, TEST_API_TOKEN, TEST_ADMIN_TOKEN)
    with TestClient(m.app) as c:
        c.main = m
        yield c


# ───────────────────────── 인증 ─────────────────────────
def test_unset_token_rejects_protected_endpoints_with_503(no_token):
    c = no_token
    assert c.post("/route/plan", json=PLAN).status_code == 503
    assert c.get("/report/accessibility").status_code == 503
    assert c.post("/report/accessibility", json={"lat": 37.39, "lng": 126.95, "reason": "curb"}).status_code == 503
    assert c.delete("/report/accessibility/1").status_code == 503
    assert c.get("/meta/latency").status_code == 503
    # 아무 토큰이나 보내도 통과하지 않는다
    assert c.post("/route/plan", json=PLAN, headers={"Authorization": "Bearer anything"}).status_code == 503
    assert c.post("/admin/reload-overrides", headers={"Authorization": "Bearer anything"}).status_code == 503
    assert c.post("/admin/reload-network").status_code == 503


def test_public_endpoints_stay_open_without_token(no_token):
    """컨테이너 헬스체크·상태 확인·API 문서는 토큰 없이 응답한다."""
    c = no_token
    h = c.get("/health")
    assert h.status_code == 200 and h.json()["graph_loaded"] is True
    assert c.get("/meta/network").status_code == 200
    assert c.get("/profiles").status_code == 200
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/docs").status_code == 200


def test_wrong_or_missing_token_is_401(api_only):
    c = api_only
    for hdr in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TEST_API_TOKEN},
                {"Authorization": "Basic %s" % TEST_API_TOKEN},
                {"Authorization": "Bearer %sx" % TEST_API_TOKEN},
                {"Authorization": "Bearer %s" % TEST_API_TOKEN[:-1]}):
        r = c.post("/route/plan", json=PLAN, headers=hdr)
        assert r.status_code == 401, (hdr, r.status_code)
        assert r.headers.get("www-authenticate") == "Bearer"


def test_correct_token_is_200(api_only):
    c = api_only
    assert c.post("/route/plan", json=PLAN, headers=API_AUTH).status_code == 200
    # 스킴 대소문자는 가리지 않는다
    r = c.post("/route/plan", json=PLAN, headers={"Authorization": "bearer %s" % TEST_API_TOKEN})
    assert r.status_code == 200
    assert c.get("/report/accessibility", headers=API_AUTH).status_code == 200


def test_admin_endpoints_503_when_admin_token_unset(api_only):
    """관리 토큰이 설정되지 않은 서버 — 일반 토큰이 맞아도 관리 엔드포인트는 503."""
    assert api_only.post("/admin/reload-overrides", headers=API_AUTH).status_code == 503
    assert api_only.post("/admin/reload-network", headers=API_AUTH).status_code == 503


def test_admin_endpoints_need_admin_token(full):
    # 둘 다 설정된 서버 — 일반 토큰 401, 관리 토큰 200
    assert full.post("/admin/reload-overrides", headers=API_AUTH).status_code == 401
    assert full.post("/admin/reload-network", headers=API_AUTH).status_code == 401
    assert full.post("/admin/reload-overrides", headers=ADMIN_AUTH).status_code == 200
    assert full.post("/admin/reload-network", headers=ADMIN_AUTH).status_code == 200
    # 관리 토큰은 일반 엔드포인트용이 아니다
    assert full.post("/route/plan", json=PLAN, headers=ADMIN_AUTH).status_code == 401


def test_report_review_and_delete_use_general_token(full):
    """제보 검토(PATCH)·삭제(DELETE)는 관리 콘솔이 일반 토큰을 보내 호출한다 — 관리 토큰을 요구하지 않는다."""
    r = full.post("/report/accessibility", headers=API_AUTH,
                  json={"lat": 37.3900, "lng": 126.9505, "reason": "curb"})
    rid = r.json()["report_id"]
    assert full.patch("/report/accessibility/%d" % rid, json={"action": "confirm"}).status_code == 401
    assert full.patch("/report/accessibility/%d" % rid, json={"action": "confirm"},
                      headers=API_AUTH).status_code == 200
    assert full.delete("/report/accessibility/%d" % rid, headers=API_AUTH).status_code == 200


# ───────────────────────── 제보 사진 ─────────────────────────
def _report(c, photo_b64=None, mime=None):
    body = {"lat": 37.3900, "lng": 126.9505, "reason": "curb"}
    if photo_b64 is not None:
        body["photo_base64"] = photo_b64
    if mime is not None:
        body["photo_mime"] = mime
    return c.post("/report/accessibility", json=body, headers=API_AUTH)


def _assert_photo_headers(r):
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == CSP


@pytest.mark.parametrize("raw,sent_mime,expect", [
    (PNG, "image/png", "image/png"),
    (JPEG, "image/jpeg", "image/jpeg"),
    (JPEG, "image/jpg", "image/jpeg"),             # 흔한 비표준 표기는 표준 이름으로
    (JPEG, None, "image/jpeg"),                    # 형식을 안 보내도 판정값으로 채운다
    (WEBP, "image/webp", "image/webp"),
])
def test_valid_photo_is_stored_and_served_as_image(full, raw, sent_mime, expect):
    r = _report(full, _b64(raw), sent_mime)
    assert r.status_code == 200, r.text
    rid = r.json()["report_id"]
    items = full.get("/report/accessibility", headers=API_AUTH).json()["items"]
    assert [i["photo_mime"] for i in items if i["report_id"] == rid] == [expect]
    p = full.get("/report/accessibility/%d/photo" % rid, headers=API_AUTH)
    assert p.status_code == 200
    assert p.content == raw
    assert p.headers["content-type"] == expect
    assert p.headers["content-disposition"].startswith("inline;")
    _assert_photo_headers(p)


def test_data_url_photo_from_navigation_screen(full):
    """이동경로 안내 화면이 보내는 형태 — canvas JPEG 의 data URL 본문 + image/jpeg."""
    r = _report(full, "data:image/jpeg;base64," + _b64(JPEG), "image/jpeg")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("raw,sent_mime", [
    (b"<html><body>not an image</body></html>", "image/jpeg"),   # 이미지라고 신고한 문서
    (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"),
    (PNG, "image/jpeg"),                                         # 신고 형식과 실제 형식 불일치
    (JPEG, "text/html"),                                         # 허용 목록 밖 신고 형식
    (JPEG, "image/gif"),
    (b"GIF89a" + b"\x00" * 16, None),                            # 허용 목록 밖 실제 형식
])
def test_photo_type_mismatch_is_400_and_not_stored(full, raw, sent_mime):
    r = _report(full, _b64(raw), sent_mime)
    assert r.status_code == 400, (r.status_code, r.text)
    assert full.get("/report/accessibility", headers=API_AUTH).json()["count"] == 0


def test_photo_base64_errors_stay_422(full):
    assert _report(full, "@@@@", "image/jpeg").status_code == 422
    from route_service.collect.store import MAX_PHOTO_B64_CHARS
    r = _report(full, "A" * (MAX_PHOTO_B64_CHARS + 4), "image/jpeg")   # 본문 검증 단계 길이 상한
    assert r.status_code == 422


def test_photo_length_cap_covers_max_decoded_size():
    """길이 상한은 2MB 사진의 base64(76자 줄바꿈 포함 + data URL 접두)를 담는다."""
    from route_service.collect.store import MAX_PHOTO_B64_CHARS, MAX_PHOTO_BYTES
    b64 = base64.encodebytes(b"\xff" * MAX_PHOTO_BYTES).decode().replace("\n", "\r\n")
    assert len("data:image/jpeg;base64," + b64) <= MAX_PHOTO_B64_CHARS


def test_legacy_non_image_photo_is_served_as_download(full, monkeypatch):
    """형식 검사 이전에 저장된 사진 — 저장된 형식값을 쓰지 않고 내려받기로 돌린다."""
    store = full.main.collect_store.STORE
    monkeypatch.setattr(store, "get_report_photo",
                        lambda rid: (b"<html><body>legacy</body></html>", "text/html"))
    p = full.get("/report/accessibility/7/photo", headers=API_AUTH)
    assert p.status_code == 200
    assert p.headers["content-type"] == "application/octet-stream"
    assert p.headers["content-disposition"].startswith("attachment;")
    _assert_photo_headers(p)
    # 저장값이 이미지라고 해도 바이트가 이미지가 아니면 같다
    monkeypatch.setattr(store, "get_report_photo", lambda rid: (b"plain text", "image/png"))
    p = full.get("/report/accessibility/7/photo", headers=API_AUTH)
    assert p.headers["content-type"] == "application/octet-stream"
    _assert_photo_headers(p)


def test_photo_endpoint_requires_token(full):
    rid = _report(full, _b64(PNG), "image/png").json()["report_id"]
    assert full.get("/report/accessibility/%d/photo" % rid).status_code == 401
