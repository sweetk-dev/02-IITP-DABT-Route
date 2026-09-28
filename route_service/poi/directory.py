# -*- coding: utf-8 -*-
"""장애인 서비스 제공기관 · 장애인 표준사업장 목록 (v1.31.0).

통합DB 에 적재돼 있으나 비서가 쓰지 않던 목록 두 가지를 조회한다. 좌표가 없는 명부라
거리 계산은 하지 않고, 구(區)·서비스 종류·이름으로 좁힌다.

  · 서비스 제공기관 — ``selfdiag_provider``(안양, 발달재활 바우처 사용처·주간활동·거주시설 등)에
    ``emp_dis_dev_support_org``(발달장애인 지원기관의 제공 서비스 10종 표시)를 이름으로 합친다.
  · 장애인 표준사업장 — ``emp_dis_std_workplace``(한국장애인고용공단 인증, 업종·주소·전화).

두 명부 모두 정기 갱신이 아니다(적재 시점이 응답의 ``base_date``). 기관 사정은 바뀔 수 있으므로
비서는 "전화로 확인" 을 함께 안내한다.
"""
from __future__ import annotations

import re

from .store import sigungu_variants

DEV_ORG_SERVICES = {
    "day_activity": "주간활동", "afterschool": "방과후활동", "indiv_plan": "개인별지원계획",
    "parent_edu": "부모교육", "family_rest": "가족휴식", "parent_counsel": "부모상담",
    "rights_relief": "권리구제", "public_guardian": "공공후견", "child_family_sup": "아동가족지원",
    "emergency_care": "긴급돌봄",
}
# 이용자 표현 → 서비스 명칭에 들어 있는 낱말
SERVICE_ALIASES = {
    "주간활동": ("주간활동",), "방과후": ("방과후",), "발달재활": ("발달재활",),
    "언어": ("언어발달",), "거주": ("거주시설",), "활동지원": ("활동지원",),
    "직업재활": ("직업 재활", "직업재활"), "재활": ("재활",), "긴급돌봄": ("긴급돌봄",),
    "부모": ("부모교육", "부모상담"), "후견": ("공공후견",), "권리": ("권리구제",),
    "휴식": ("가족휴식",), "바우처": ("바우처",),
}
PROVIDER_SOURCE = "장애인 자립 지원 서비스 제공기관 명부"
DEVORG_SOURCE = "발달장애인 지원기관 현황"
WORKPLACE_SOURCE = "한국장애인고용공단 장애인 표준사업장 인증 현황"


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _key(s) -> str:
    s = re.sub(r"\(주\)|㈜|주식회사|\s|안양", "", str(s or ""))
    return re.sub(r"[()·.,\-]", "", s)


def _service_words(service: str) -> tuple:
    s = (service or "").strip()
    if not s:
        return ()
    for k, words in SERVICE_ALIASES.items():
        if k in s:
            return words
    return (s,)


def _district_ok(addr: str, district: str) -> bool:
    d = (district or "").strip()
    if not d:
        return True
    d = d[:-1] if d.endswith("구") else d
    return d in (addr or "")


def _date(v):
    return str(v)[:10] if v else None


def providers(store, service: str = "", district: str = "", q: str = "",
              sigungu: str = "안양", limit: int = 10) -> dict:
    """서비스 제공기관. 같은 기관의 여러 서비스 행은 하나로 묶는다."""
    if store.backend == "none":
        return {"items": [], "count": 0, "total": 0, "base_date": None}
    # selfdiag_provider 는 안양 명부다(주소에 "안양"이 빠진 행이 많아 주소로 거를 수 없다)
    anyang = "안양" in (sigungu or "")
    if store.backend == "file":
        prov = store._load_file("selfdiag_provider.json") if anyang else []
        dev = [r for r in store._load_file("dev_support_org.json")
               if (sigungu or "") in (r.get("region") or "")]
    else:
        prov = store._query(
            "SELECT provider_name, service_name, address, phone, description, created_at"
            "  FROM selfdiag_provider", {}) if anyang else []
        variants = sigungu_variants(sigungu) or [""]
        dev = store._query(
            "SELECT org_name, region, " + ", ".join(DEV_ORG_SERVICES) + ", created_at"
            "  FROM emp_dis_dev_support_org WHERE region LIKE ANY(:pats)",
            {"pats": ["%%%s%%" % v for v in variants]})
    groups = {}
    base = []
    for r in prov:
        name = _clean(r.get("provider_name"))
        if not name:
            continue
        addr = _clean(r.get("address"))
        k = _key(name)
        g = groups.setdefault(k, {"name": name, "addr": addr, "tel": None, "services": [],
                                  "sources": [PROVIDER_SOURCE]})
        svc = _clean(r.get("service_name"))
        if svc and svc not in g["services"]:
            g["services"].append(svc)
        if not g["tel"] and _clean(r.get("phone")):
            g["tel"] = _clean(r.get("phone"))
        if len(addr) > len(g["addr"] or ""):
            g["addr"] = addr
        base.append(r.get("created_at"))
    for r in dev:
        name = _clean(r.get("org_name"))
        if not name:
            continue
        svcs = [label for col, label in DEV_ORG_SERVICES.items() if r.get(col) in (True, "t", "Y", 1)]
        k = _key(name)
        hit = next((gk for gk in groups if gk and k and (gk in k or k in gk)), None)
        if hit is None:
            groups[k] = {"name": name, "addr": _clean(r.get("region")), "tel": None,
                         "services": [], "sources": [DEVORG_SOURCE]}
            hit = k
        g = groups[hit]
        for s in svcs:
            if s not in g["services"]:
                g["services"].append(s)
        if DEVORG_SOURCE not in g["sources"]:
            g["sources"].append(DEVORG_SOURCE)
        base.append(r.get("created_at"))
    words = _service_words(service)
    qk = _key(q)
    out = []
    for g in groups.values():
        if words and not any(w in s for s in g["services"] for w in words):
            continue
        if not _district_ok(g["addr"], district):
            continue
        if qk and qk not in _key(g["name"]):
            continue
        out.append(g)
    out.sort(key=lambda g: (g["tel"] is None, g["name"]))
    base = [b for b in base if b]
    return {"items": out[:limit], "count": min(len(out), limit), "total": len(out),
            "base_date": _date(max(base)) if base else None}


def workplaces(store, sigungu: str = "안양", q: str = "", district: str = "", limit: int = 10) -> dict:
    """장애인 표준사업장 — 주소의 시·군·구, 업종·이름 낱말, 구로 좁힌다."""
    if store.backend == "none":
        return {"items": [], "count": 0, "total": 0, "base_date": None}
    if store.backend == "file":
        rows = store._load_file("std_workplace.json")
    else:
        variants = sigungu_variants(sigungu) or [""]
        rows = store._query(
            "SELECT company_name, address, tel, business_item, type, cert_date, created_at"
            "  FROM emp_dis_std_workplace WHERE address LIKE ANY(:pats)",
            {"pats": ["%%%s%%" % v for v in variants]})
    variants = sigungu_variants(sigungu)
    qq = (q or "").strip()
    out, base = [], []
    for r in rows:
        addr = _clean(r.get("address"))
        if variants and not any(v in addr for v in variants):
            continue
        if not _district_ok(addr, district):
            continue
        name = _clean(r.get("company_name"))
        item = _clean(r.get("business_item"))
        if qq and qq not in name and qq not in item:
            continue
        out.append({"name": name, "addr": addr, "tel": _clean(r.get("tel")) or None,
                    "business": item or None, "type": _clean(r.get("type")) or None,
                    "cert_date": _date(r.get("cert_date")), "source_label": WORKPLACE_SOURCE})
        base.append(r.get("created_at"))
    out.sort(key=lambda x: x["name"])
    base = [b for b in base if b]
    return {"items": out[:limit], "count": min(len(out), limit), "total": len(out),
            "base_date": _date(max(base)) if base else None}
