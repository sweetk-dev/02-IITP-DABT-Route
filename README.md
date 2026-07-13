# 02-IITP-DABT-Route

주변 장소 탐색 및 최적 경로 추천 모듈 — **무장애 보행 경로 추천 API**

장애 유형별 통행 프로필(경사·계단·육교·보도폭)을 적용해 경로를 탐색하고, 지도에 그릴 좌표열과
음성으로 읽어줄 턴바이턴 안내를 반환한다. 12-IITP-DABT-AccessistantAI(장애인 AI 비서)가
도구 호출로 사용한다.

## 버전

| 레포 | 버전 |
|---|---|
| 02-IITP-DABT-Route | v1.2.0 |

## 구조

```
route_service/
  config.py            환경변수 설정
  engine/
    profiles.py        장애 유형별 통행 프로필 (수동/전동 휠체어·목발·시각장애·일반)
    graph.py           표준 그래프 스키마 · 상주 스토어(무중단 교체)
    snap.py            GPS 좌표 -> 보행 노드 스냅
    planner.py         A* 경로 탐색 · 대안 경로 · 제약 완화 폴백 · 이탈 판정
    steps.py           턴바이턴 안내 문장 생성
    dem.py             DEM(.img) 기반 링크 경사 부여
    sources/
      osm.py           OpenStreetMap 보행망 어댑터
      tabular.py       node/link 표(xlsx·csv) 어댑터
  poi/store.py         무장애 관광지 · 대중교통 접근점 (db | file | none)
  api/main.py          FastAPI
scripts/
  build_network.py     그래프 구축 CLI
  planning.py          배치 경로 탐색 CLI (기존 스크립트 호환)
tests/                 pytest (합성 보행망 기반)
```

## 설치 · 실행

```bash
pip install -r requirements.txt          # 서비스 구동
pip install -r requirements-build.txt    # 그래프 구축 시에만

cp .env.example .env                     # 값 채우기 (레포에 커밋 금지)
# 안양 보행망 + 공개DEM 으로 그래프 구축
python scripts/build_network.py --source osm \
    --place "Anyang-si, Gyeonggi-do, South Korea" \
    --dem data/dem/anyang_37612_90m.img \
    --out data/network_anyang.gpickle --version anyang-osm-dem90-2026Q3

# 품질 확인 (경사 커버리지가 0 이면 경사 회피가 동작하지 않는다)
python scripts/inspect_network.py --network data/network_anyang.gpickle \
    --route 37.4025,126.9227,37.3856,126.9256

uvicorn route_service.api.main:app --host 0.0.0.0 --port 18100
```

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/health` | 헬스체크 (그래프 로드 여부·POI 백엔드) |
| GET | `/meta/network` | 네트워크 메타 (노드·링크 수, bbox, **경사/링크타입 커버리지**) |
| GET | `/profiles` | 통행 프로필 목록 |
| POST | `/route/plan` | **경로 탐색** — 요약·좌표열·턴바이턴 스텝·대안 경로 |
| POST | `/route/reroute` | 경로 이탈 시 재탐색 |
| POST | `/route/snap` | 좌표 -> 보행 노드 스냅 |
| GET | `/route/{route_id}` | 직전 경로 조회(캐시) |
| GET | `/tour/bf-spots` | 무장애 관광지 목록 |
| GET | `/tour/bf-spots/{id}` | 관광지 상세 (편의시설 Y/N) |
| GET | `/tour/bf-spots/{id}/entrance` | **무장애 출입구 좌표** (없으면 시설 대표 좌표로 대체 표기) |
| POST | `/tour/recommend` | 장애 유형별 관광지 추천 랭킹 |
| GET | `/transit/access-points` | 휠체어 접근 가능한 정류장·역 |
| POST | `/admin/reload-network` | 그래프 무중단 교체 |

### 경로 탐색 예시

```bash
curl -X POST localhost:18100/route/plan -H "Content-Type: application/json" -d '{
  "origin": {"lat": 37.3943, "lng": 126.9568},
  "destination": {"type": "tour", "poi_id": "TBF-0001"},
  "profile": "wheelchair_manual",
  "alternatives": 2
}'
```

응답 요약

```json
{
  "route_id": "r_ab12cd34ef",
  "routes": [{
    "summary": {"total_distance_m": 1240, "duration_sec": 1771, "max_slope_deg": 3.6,
                "stairs_cnt": 0, "crossing_cnt": 4, "accessibility_score": 0.86},
    "geometry": [[37.3943, 126.9568], "..."],
    "steps": [{"idx": 0, "maneuver": "depart", "instruction": "중앙로를 따라 120m 앞으로 이동합니다.",
               "distance_m": 120, "coord": [37.3943, 126.9568], "warnings": []}]
  }],
  "fallback": {"used": false},
  "data_quality": {"slope_coverage": 0.98, "link_type_available": true}
}
```

## 통행 프로필

| id | 대상 | 최대 경사 | 회피 |
|---|---|---|---|
| `wheelchair_manual` | 수동 휠체어 (기본값) | 4.0° | 계단·육교·지하보도 |
| `wheelchair_electric` | 전동 휠체어 | 6.0° | 계단·육교·지하보도 |
| `crutch` | 목발·보행보조 | 8.0° | 육교 (계단은 비용 가중) |
| `visual` | 시각장애 | 12.0° | — (육교·지하보도 비용 가중) |
| `walk` | 일반 보행 | 20.0° | — |

제약을 만족하는 경로가 없으면 경사 한계를 단계적으로 완화해 재탐색하고, 응답의
`fallback` 에 완화 사실과 사유를 표기한다(경로 없음으로 끝내지 않는다).

## 대중교통

대중교통 **구간 라우팅은 범위 밖**이다. 휠체어로 **대중교통을 이용하는 지점까지** 가는
보행 경로를 제공한다.

- 목적지 유형 `transit_station`: 지하철역 — 승강설비(엘리베이터/리프트) 보유 여부로 접근성 판정
- 목적지 유형 `transit_stop`: 버스 정류장 — 저상버스 정차 여부는 정적 데이터에 없으므로
  실시간 도착정보(GBIS `lowPlate`)로 확인해야 한다

## 데이터

| 구분 | 현재 | 비고 |
|---|---|---|
| 보행 네트워크 | **OSM 안양 보행망** — 노드 6,750 / 링크 9,712 | 원본(node/link) 수령 시 `--source tabular` 로 재구축 후 `/admin/reload-network` — API 스키마 불변 |
| 경사 | **국토지리정보원 공개DEM 90m**(도엽 37612) — 커버리지 99.5% | 5m DEM 확보 시 `--dem` 만 교체. DEM 이 없으면 `--elevation terrain`(공개 지형 타일, 인증 불필요) |
| 무장애 관광지·역·정류장 | 01-IITP-DABT-Database (`POI_BACKEND=db`) | 파이프라인 적재 전에는 `file`/`none` 백엔드로 기동 가능 |

### 안양 그래프 실측 (v1.2.0)

| 링크 타입 | 비율 | | 경사 | |
|---|---|---|---|---|
| 도로·이면도로 | 71.1% | | 4° 초과(수동 휠체어 통행 불가) | **1,087개 (11.2%)** |
| 보도 | 20.9% | | 최대 | 28.0° |
| 횡단보도 | 4.3% | | 경사 커버리지 | 99.5% |
| 계단·육교·지하보도 | 1.4% | | 보도폭 커버리지 | 0.1% (OSM 한계) |

경로 비교 (안양역 → 안양예술공원)

| 프로필 | 거리 | 최대 경사 | 계단 |
|---|---|---|---|
| 수동 휠체어 | 2,390m | **3.21°** | 0 |
| 일반 보행 | 2,385m | 4.78° | 0 |

+5m 우회로 급경사 구간을 회피한다. 보도폭·턱낮춤은 OSM 태그 커버리지가 낮아
현재는 판정에 거의 기여하지 못한다 — 융기원 원본 데이터에서 보강해야 하는 항목이다.

데이터 품질은 `/meta/network` 와 경로 응답의 `data_quality` 로 항상 노출한다. 계단·경사 속성이
없는 네트워크에서는 회피 판정이 성립하지 않으므로 반드시 확인할 것.

## 테스트

```bash
python -m pytest -q
```
