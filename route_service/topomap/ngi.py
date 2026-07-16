# -*- coding: utf-8 -*-
"""NGI/NDA(국토지리정보원 수치지도 교환포맷) 리더.

GDAL 에 NGI 드라이버가 없어 자체 파싱한다.
- .ngi : 레이어별 지오메트리 (POINT / LINESTRING / POLYGON, NUMPARTS 지원)
- .nda : 레이어별 속성 ($ASPATIAL_FIELD_DEF + $RECORD n)
두 파일은 레이어명 + $RECORD 번호로 조인한다. 인코딩은 CP949.
"""
from __future__ import annotations

import re
from typing import Iterator

_NUM = re.compile(r"^-?\d+\.?\d*(?:[eE][-+]?\d+)?$")


def _read(path: str) -> list[str]:
    with open(path, "r", encoding="cp949", errors="replace") as f:
        return f.read().splitlines()


def _split_layers(lines: list[str]) -> Iterator[tuple[str, list[str]]]:
    """<LAYER_START> ... 다음 <LAYER_START> 전까지를 한 레이어로 자른다."""
    starts = [i for i, l in enumerate(lines) if l.strip() == "<LAYER_START>"]
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        body = lines[s:e]
        name = ""
        for i, l in enumerate(body):
            if l.strip() == "$LAYER_NAME":
                name = body[i + 1].strip().strip('"')
                break
        yield name, body


def _parse_ngi_geoms(body: list[str]) -> dict[int, dict]:
    """레이어 본문 -> {record_no: {'type':..., 'parts':[[(x,y),...], ...]}}"""
    out: dict[int, dict] = {}
    i = 0
    n = len(body)
    cur = None
    while i < n:
        s = body[i].strip()
        m = re.match(r"^\$RECORD\s+(\d+)$", s)
        if m:
            cur = int(m.group(1))
            i += 1
            continue
        if cur is not None and s in ("POINT", "LINESTRING", "POLYGON", "MULTIPOINT",
                                    "MULTILINESTRING", "MULTIPOLYGON", "TEXT"):
            gtype = s
            i += 1
            nparts = 1
            if i < n and body[i].strip().startswith("NUMPARTS"):
                nparts = int(body[i].split()[1]); i += 1
            parts = []
            for _ in range(nparts):
                # 좌표 개수 줄
                while i < n and not _NUM.match(body[i].strip().split(" ")[0] or "x"):
                    i += 1
                if i >= n:
                    break
                cnt_line = body[i].strip()
                if " " in cnt_line:      # POINT 는 개수 줄 없이 바로 좌표
                    pts = []
                    xy = cnt_line.split()
                    pts.append((float(xy[0]), float(xy[1])))
                    i += 1
                    parts.append(pts)
                    continue
                cnt = int(cnt_line); i += 1
                pts = []
                for _ in range(cnt):
                    if i >= n:
                        break
                    xy = body[i].split()
                    if len(xy) >= 2 and _NUM.match(xy[0]):
                        pts.append((float(xy[0]), float(xy[1])))
                    i += 1
                parts.append(pts)
            if cur not in out and parts:
                out[cur] = {"type": gtype, "parts": parts}
            continue
        i += 1
    return out


def _parse_nda_attrs(body: list[str]) -> tuple[list[str], dict[int, list]]:
    fields: list[str] = []
    for l in body:
        m = re.match(r'^\s*ATTRIB\("([^"]*)"', l)
        if m:
            fields.append(m.group(1))
    recs: dict[int, list] = {}
    i = 0
    while i < len(body):
        m = re.match(r"^\$RECORD\s+(\d+)$", body[i].strip())
        if m and i + 1 < len(body):
            rid = int(m.group(1))
            vals = _split_csv(body[i + 1])
            recs[rid] = vals
            i += 2
            continue
        i += 1
    return fields, recs


def _split_csv(line: str) -> list[str]:
    out, buf, q = [], "", False
    for ch in line:
        if ch == '"':
            q = not q
        elif ch == "," and not q:
            out.append(buf.strip()); buf = ""
        else:
            buf += ch
    out.append(buf.strip())
    return out


def read_ngi(ngi_path: str, nda_path: str | None = None) -> dict[str, dict]:
    """{layer_name: {'fields': [...], 'features': [{'geom':..., 'attrs': {...}}]}}"""
    if nda_path is None:
        nda_path = ngi_path[:-4] + ".nda"
    geo_layers = {name: _parse_ngi_geoms(b) for name, b in _split_layers(_read(ngi_path))}
    att_layers = {}
    try:
        att_layers = {name: _parse_nda_attrs(b) for name, b in _split_layers(_read(nda_path))}
    except FileNotFoundError:
        pass
    out = {}
    for name, geoms in geo_layers.items():
        fields, recs = att_layers.get(name, ([], {}))
        feats = []
        for rid, g in sorted(geoms.items()):
            vals = recs.get(rid, [])
            attrs = dict(zip(fields, vals)) if fields else {}
            feats.append({"geom": g, "attrs": attrs})
        out[name] = {"fields": fields, "features": feats}
    return out
