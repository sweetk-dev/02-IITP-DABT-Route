# -*- coding: utf-8 -*-
"""설치 버전 고정 파일(constraints.txt) 점검 (v1.44.1)."""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read(name: str) -> list:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read().splitlines()


def _pins() -> dict:
    out = {}
    for line in _read("constraints.txt"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, ver = line.partition("==")
        assert ver, "버전이 고정되지 않은 줄: %s" % line
        out[_norm(name)] = ver
    return out


def test_every_requirement_is_pinned():
    pins = _pins()
    missing = []
    for line in _read("requirements.txt"):
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = _norm(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0])
        if name not in pins:
            missing.append(name)
    assert missing == [], "constraints.txt 에 버전이 없는 패키지: %s" % missing


def test_dockerfile_installs_with_constraints():
    installs = [l for l in _read("Dockerfile") if l.strip().startswith("RUN pip install") and "-r requirements" in l]
    assert installs and all("-c constraints.txt" in l for l in installs), installs
