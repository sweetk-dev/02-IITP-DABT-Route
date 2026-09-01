# -*- coding: utf-8 -*-
"""버전 표기 정합 검사.

``/health`` 와 OpenAPI 는 ``route_service.__version__`` 을 그대로 보고한다.
실행 중인 서비스가 어느 배포본인지 판별하는 런타임 근거이므로 레포 버전과
일치해야 한다.

버전 표기는 소스가 둘(코드 상수와 README)이라 수동으로는 갈라질 수 있다.
여기서 일치를 고정해 릴리즈 전 검증에서 자동으로 확인되도록 한다.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from route_service import __version__  # noqa: E402


def _readme_version() -> str:
    path = os.path.join(ROOT, "README.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"\|\s*02-IITP-DABT-Route\s*\|\s*v(\d+\.\d+\.\d+)\s*\|", text)
    assert m, "README '## 버전' 표에서 레포 버전을 찾지 못했다"
    return m.group(1)


def test_version_is_semver():
    """``__version__`` 은 v 접두사 없는 major.minor.patch 형식이다."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), (
        "__version__ 은 major.minor.patch 형식이어야 한다: %r" % __version__)


def test_version_matches_readme():
    """코드 상수와 README 표기가 같아야 한다."""
    readme = _readme_version()
    assert __version__ == readme, (
        "route_service/__init__.py 의 __version__(%s)과 "
        "README 표기(v%s)가 다르다. 릴리즈 시 두 곳을 함께 갱신한다."
        % (__version__, readme))
