# 02-IITP-DABT-Route

주변 장소 탐색 및 최적 경로 추천 모듈 — **무장애 보행 경로 추천 API**

장애 유형별 통행 프로필(경사·계단·육교·보도폭)을 적용해 경로를 탐색하고, 지도에 그릴 좌표열과
음성으로 읽어줄 턴바이턴 안내를 반환한다. 12-IITP-DABT-AccessistantAI(장애인 AI 비서)가
도구 호출로 사용한다.

## 버전

| 레포 | 버전 |
|---|---|
| 02-IITP-DABT-Route | v1.24.0 |

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
                       무장애 속성은 3소스 결합 — mv_poi(경기관광공사) +
                       poi_tour_bf_facility(한국관광공사) +
                       poi_facility_accessibility(한국사회보장정보원)
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
# (1) 수치지형도 등고선 -> 5m DEM 생성
python scripts/dem_from_contours.py \
    --src "<수치지도 zip 폴더>" --out data/dem/anyang_5m.tif --res 5

# (2) 안양 보행망 + 5m DEM 으로 그래프 구축
python scripts/build_network.py --source osm \
    --place "Anyang-si, Gyeonggi-do, South Korea" \
    --dem data/dem/anyang_5m.tif \
    --out data/network_anyang.gpickle --version anyang-osm-dem5-2026Q3

# 품질 확인 (경사 커버리지가 0 이면 경사 회피가 동작하지 않는다)
python scripts/inspect_network.py --network data/network_anyang.gpickle \
    --route 37.4025,126.9227,37.3856,126.9256

uvicorn route_service.api.main:app --host 0.0.0.0 --port 18100
```

### 수치지형도(1:1,000) 속성 보강

OSM 그래프의 보행 링크에 수치지형도 인도 면형의 **실측 폭·재질**을 전이한다
(휠체어 프로필 `min_width` 를 추정치가 아닌 실측치로 판정).

```bash
# (a) OSM 횡단보도 확보 (인터넷 필요)
python scripts/fetch_osm_crossings.py \
    --place "Anyang-si, Gyeonggi-do, South Korea" --out data/osm_crossings.geojson

# (b) 그래프 속성 보강 (1:1,000 우선, 1:5,000 폴백)
python scripts/enrich_osm_with_topomap.py \
    --graph data/network_anyang.gpickle \
    --src-1k "<1:1,000 도엽 폴더>" --src-5k "<1:5,000 도엽 폴더>" \
    --out data/network_anyang_enriched.gpickle
```

안양 실측(2026-07-16): 보행 링크 1,428건에 폭 채움(보도 44% · 횡단보도 76%),
폭 중앙값 3.0m. road(이면도로) 링크는 차도 보행이라 보강 대상에서 제외.

수치지형도 **단독** node/link 산출도 지원한다 (`scripts/build_pednet_from_topomap.py`,
`route_service/topomap/` — NGI/NDA 자체 파서 + 면형 중심선화 + 위상 구축).
단, 수치지형도에는 횡단보도 레이어가 없어(1:1,000·1:5,000 모두 실측 0건)
단독 그래프는 블록 단위로 끊긴다 — 연결 골격은 OSM, 수치지형도는 속성 원천으로 쓴다.

### 통합DB 적재 (mv_pednet_node / mv_pednet_link)

보강 그래프를 node/link 테이블로 산출해 통합DB(iitp_db)에 적재한다
(스키마: `scripts/sql/pednet_schema.sql`, 명명은 기존 `mv_poi` 의 `mv_` 규약).

```bash
python scripts/export_graph_tables.py \
    --graph data/network_anyang_enriched.gpickle \
    --out-dir data/db_export --version anyang-topo-enrich-2026Q3
# 이후 psql: schema 적용 -> \copy 로 CSV 적재 (동일 network_version 재적재 시 DELETE 선행)
```

### 컨테이너로 실행

그래프 구축 의존성(osmnx·rasterio·geopandas)은 이미지에 넣지 않는다. 구축은 오프라인 작업이고,
서비스는 완성된 그래프와 건물 폴리곤만 읽는다. **데이터는 볼륨으로 주입**하므로 이미지를 다시
빌드하지 않고도 그래프를 교체할 수 있다.

```bash
docker build -t route-api .
docker run -d --name route-api -p 18100:18100 \
    -v "$PWD/data:/app/data:ro" \
    -e NETWORK_PATH=/app/data/network_anyang.gpickle \
    -e BUILDINGS_PATH=/app/data/buildings_anyang.pkl \
    -e ENTRANCES_PATH=/app/data/poi/entrances.json \
    -e POI_BACKEND=db \
    -e POI_DB_DSN='postgresql+psycopg2://<user>:<pw>@<host>:5432/<db>' \
    route-api

curl -s localhost:18100/health
curl -s localhost:18100/meta/network
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
| GET | `/tour/bf-spots/{id}/entrance` | **무장애 접근 지점** — 실측 출입구 > 건물 접근점 > 시설 대표점 |
| POST | `/tour/recommend` | 장애 유형별 관광지 추천 랭킹 — `origin_lat/lng` 지정 시 **거리 오름차순**, `offset` 페이징(`total`/`has_more` 반환) |
| GET | `/transit/access-points` | 휠체어 접근 가능한 정류장·역 (역 화장실·경사로 유무는 3상태) |
| GET | `/transit/bus/arrivals` | **정류장 실시간 도착정보** — 노선별 1·2번째 차량의 도착 예정·정거장 수·**저상 여부**, `next_low_floor` |
| GET | `/transit/bus/locations` | **노선 실시간 차량 위치** — 운행 차량의 현재 정류장(좌표 조인)·저상 여부 |
| GET | `/transit/station/facilities` | **역 편의시설(설비 단위)** — 승강기·리프트 출입구/상세위치, 화장실(게이트 안/밖), 승강장(안전발판·이격거리) |
| POST | `/track/log` | 주행 GPS 트랙 적재 (참여자 식별자 없음 — `route_id` 익명) |
| POST | `/report/accessibility` | 접근성 오류 제보 — 접수 즉시 '이용자 제보(미확인)' 경고 부착 |
| GET | `/report/accessibility` | 제보 목록 (관리 콘솔용) |
| GET | `/report/accessibility/{id}/photo` | 제보 사진 |
| PATCH | `/report/accessibility/{id}` | 검토 — confirm(확정) / reject(기각) / apply(속성 반영, 승인제) |
| DELETE | `/report/accessibility/{id}` | **제보 삭제** — 오검·중복·시험 제보 정리. 파생 오버라이드도 함께 사라진다 |
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

## 목적지 접근 지점 (무장애 출입구)

POI 좌표는 **시설 대표점(건물 중심)** 이다. 그대로 목적지로 쓰면 건물 뒤편 도로에 스냅되어,
"도착했습니다" 라고 안내한 지점에서 실제 출입구까지 휠체어로 건물을 한 바퀴 돌아야 하는
상황이 생긴다. 그래서 목적지를 3단계로 해석하고, **무엇으로 정했는지 응답에 반드시 남긴다.**

| 우선순위 | `resolved_by` | 근거 |
|---|---|---|
| 1 | `manual_survey` | 현장 실측 출입구 (`data/poi/entrances.json`) — 답사 결과를 넣으면 최우선 |
| 2 | `accessible_entrance` | 데이터에 등록된 출입구 좌표 |
| 3 | `building_access` | 건물 외곽선 중 **보행망에 가장 가까운 지점** (프로필상 통행 가능한 링크만 후보) |
| 4 | `facility_centroid` | 시설 대표점 — "도착 후 출입구를 확인하세요" 경고와 함께 반환 |

### 데이터 실측 (2026-07-13, 안양)

| 소스 | 결과 |
|---|---|
| OSM `entrance` 노드 | 안양 전역 86개, `wheelchair` 태그 **0개**, 관광지 13곳 중 50m 이내 매칭 **0곳** → 사용 불가 |
| OSM 건물 폴리곤 | **9,563개** → 사용 |
| 무장애 관광지 13곳 해석 | `building_access` **8곳** / `facility_centroid` 5곳(공원·시장 등 폴리곤 없음) |

`building_access` 는 실제 출입구가 아니라 **건물의 도로 면**이다. 정확한 출입구는 현장 실증
답사로 확보해 `entrances.json` 에 등록하는 것이 정답이며, 그때까지는 응답의 `note` 로
한계를 사용자에게 알린다.

```bash
# 건물 폴리곤 수집 (그래프 구축 시 함께)
python scripts/build_network.py --source osm --place "Anyang-si, ..." \
    --dem data/dem/anyang_5m.tif --out data/network_anyang.gpickle \
    --buildings data/buildings_anyang.pkl
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

### 스냅은 "도달 가능한 덩어리" 안에서만 한다

계단·급경사를 걷어내면 보행망은 조각난다 — **안양 실측: 수동 휠체어 4° 기준 522개 컴포넌트**
(최대 5,900노드 / 나머지는 28노드 이하). 가장 가까운 통행 가능 노드에 스냅하면 그 노드가
고립 조각에 속해 실제로는 갈 수 있는 목적지가 "경로 없음" 이 된다. 그래서 프로필별
**최대 연결요소**를 계산해 그 안에서만 스냅한다(조금 더 걸어서라도 갈 수 있는 곳으로 붙인다).

## 대중교통

대중교통 **구간 라우팅은 범위 밖**이다. 휠체어로 **대중교통을 이용하는 지점까지** 가는
보행 경로를 제공한다.

- 목적지 유형 `transit_station`: 지하철역 — 승강설비(엘리베이터/리프트) 보유 여부로 접근성 판정
- 목적지 유형 `transit_stop`: 버스 정류장 — 저상버스 정차 여부는 정적 데이터에 없다.
  **v1.19.0 부터 실시간 도착정보(GBIS `lowPlate`)를 서비스가 직접 조회한다** — 아래 "실시간 버스" 참고

### 실시간 버스 (v1.19.0)

경기버스정보(GBIS) 공공데이터포털 API 두 종을 `DATA_GO_KR_API_KEY` 하나로 호출한다.

| 엔드포인트 | 원천 | 쓰임 |
|---|---|---|
| `GET /transit/bus/arrivals?station_id=&route_id=` | 버스도착정보 조회(`getBusArrivalListv2`) | 승차 정류장에 오는 노선별 1·2번째 차량의 도착 예정(분)·몇 정거장 전·**저상 여부**. `next_low_floor` = 가장 빨리 오는 저상 차량 |
| `GET /transit/bus/locations?route_id=` | 버스위치정보 조회(`getBusLocationListv2`) | 노선 전 차량의 현재 정류장 순번·저상 여부. 정적 경유정류소와 조인해 좌표를 붙인다 |
| `POST /route/plan` + `realtime: true` | 위 도착정보 | 버스 leg 의 승차 정류장 도착정보를 `legs[].realtime` 에 싣고, 저상 차량이 확인되면 고정 경고를 실측 문구로 바꾼다 |

- 실시간 조회 실패는 `status: unavailable` 로 답하고 **경로 안내는 막지 않는다**(고정 경고 유지).
- 같은 정류장·노선의 반복 폴링은 20초 TTL 캐시로 흡수한다(`GBIS_CACHE_TTL_SEC`). 개발계정 도착정보 한도는 1,000회/일이다.
- `next_low_floor` 가 `null` 인 것은 "저상버스가 없다"가 아니라 "도착정보에 잡힌 두 대 안에는 없다"는 뜻이다. 3번째 이후 차량은 위치정보로 본다.
- 스텝(`bus_board`/`bus_alight`)에 `leg_ref{route_id, board_station_id, alight_station_id}` 가 실린다 — 클라이언트가 안내 중 폴링할 키다.

### 링크 투영 스냅 — 출발·도착을 링크 위의 점에 (v1.23.0)

종전에는 출발·도착 좌표를 최근접 **노드**에만 붙였다. 링크 길이 중앙값 47m, 100m 초과 링크가 2,250개인 그래프에서는
좌표가 긴 링크 중간에 있으면 링크 한쪽 끝까지 갔다가 되돌아오는 경로가 나온다(안양아트센터 → 안양문화원 1,283m / 직선 826m).
이제 최근접 **링크 위의 점**에 요청 단위 가상 노드를 만들어 붙이고 링크 양쪽으로 나갈 수 있게 한다(`engine/vsnap.py`). 공유 그래프는 바뀌지 않는다.

- 후보: 반경 60m 안 링크에 투영, 가까운 순 3개 + 노드 스냅 폴백. 출발은 도착 최근접 후보를 기준으로, 도착은 고른 출발을 기준으로 **"투영 거리 + 경로 비용"이 최소**인 후보를 택한다(막다른 링크 회피).
- 안전 필터: 반경 20m 안에 보행 링크(sidewalk 계열)가 있으면 도로 링크 제외 · `crossing`·`steps`·`overpass`·`underpass` 는 투영 금지 · 좌표→투영점 선분이 건물 폴리곤(또는 옹벽·담장 색인)을 가로지르면 탈락(LOS) · 링크 끝 1m 이내면 끝 노드 그대로.
- 실측 출입구(`manual_survey`·`accessible_entrance`)로 해석된 도착점은 종전대로 노드 스냅 — 출입구 스퍼 끝이 곧 안내 종점이다.
- 응답 `origin.snap_kind`·`destination.snap_kind`(`edge`|`node`)·`snap_dist_m`. 끄려면 `EDGE_SNAP=false`(`EDGE_SNAP_RADIUS_M`·`EDGE_SNAP_K`).
- 실측(anyang-hybrid-2026Q3): 안양아트센터 → 안양문화원 1,283m → **1,060m(우회비 1.55 → 1.28)**, 첫 안내가 목적지 방향. 요청당 약 +40ms.

### 우회 삼각형 직결 링크 정제 — 규칙만으로 (v1.23.0)

A–M, M–B 인접 링크는 있는데 A–B 직결이 없어 짧은 거리를 크게 우회하는 지점(안양문화원 앞 10.6m → 21.8m, 급회전 안내)에
**사람 검수 없이** 직결 링크를 신설한다. 안전은 하드 게이트가, 선호는 신뢰도(비용)가 맡는다.

```bash
python scripts/build_topomap_layers.py --src "<1:1000 도엽 폴더>" --out data/topomap_layers          # 인도 면형·장애물 (한 번)
python scripts/refine_detour_links.py --graph data/network_anyang_hybrid.gpickle --layers data/topomap_layers \
    --dem data/dem/anyang_5m.tif --report data/refine_report.csv \
    --out data/network_anyang_hybrid_r2.gpickle --version anyang-hybrid-2026Q3r2
```

- 후보: 차수 무관, 우회비 ≥ 1.41·우회량 < 20m·직선 ≤ 25m.
- 하드 게이트(하나라도 걸리면 생성 안 함): 인도 면형 밖(도엽 이음새 0.3m 흡수) · 계단·옹벽·담장·가로시설물 저촉 · 점형 지물 통과 간격 0.8m 미만 · 신설선 종단경사 8° 초과 · 지그재그 경사로(고도차 1m 초과 & 인접 경사 5% 초과) · 우회 구간이 횡단보도 링크.
- 신뢰도 c(1.0 에서 곱셈 감점): 신설선 내부(양 끝 1m 제외)가 경계 인셋 0.2m 밖 ×0.55 · 보도 폭 1.5m 미만 ×0.70 · A·B 가 **맞닿지 않은** 서로 다른 폴리곤 ×0.60 · 접합각 60° 미만 ×0.70. 인셋 0.2m 는 1:1000 도화 허용오차와 휠체어 반폭 근거.
- 반영: `topo_source='derived'`·`confidence=c` 로 **추가만** 한다(기존 링크·지그재그 경사로는 그대로). 라우팅은 프로필별 활성 하한(수동 0.60·전동 0.55·시각 0.70·도보 0.40) 미만이면 없는 링크로 보고, 통과분은 `length × (1 + 4(1 − c))` 비용으로 저울질한다.
- 안양 실측(2026-09-07): 후보 414 → 인도 면형 밖 237(대부분 인도 없는 이면도로) · 횡단보도 우회 173 · **채택 4**(c 1.0·1.0·0.7·0.55). 안양문화원 앞 직결 링크 c=1.0 → 모든 프로필이 10.6m 직결로 지난다. 보고서 `data/refine_report.csv`.
- 회귀(실증 구간 9건): 거리 불변 또는 단축(안양아트센터 → 안양문화원 1,283 → 1,060m), 계단 관통 링크 증가 0.

#### 이면도로 횡단 교량 (v1.23.1)

우회 삼각형과는 다른 결함. 좁은 이면도로 양쪽의 보도 끝이 십수 m 거리인데 횡단 링크가 없어 블록을 한 바퀴 돈다
(안양문화원 → 소방서 정류장: 직선 61m 를 374m). 보도 면형 안이 아니므로 위 게이트로는 절대 통과하지 못해 별도 규칙을 둔다.
같은 스크립트가 `<report>_gaps.csv` 를 함께 내고 `--out` 이면 같이 반영한다.

- 후보: 양 끝이 수치지형도 보도 노드, 직선 3~20m, 둘 사이 보행망 경로가 없거나 직선의 4배 이상.
- 게이트: 신설선이 도로 링크를 **정확히 하나**만 가로지르고 그 도로가 이면도로(이름이 없거나 `…길`, 간선 `…로`·`…대로`는 제외) · 장애물 저촉 없음 · 종단경사 8° 이하.
- 반영: `link_type='crossing'`·`unmarked=True`·`confidence=0.65`. 수동(0.60)·전동·도보는 지나고 **시각장애(0.70)는 표시된 횡단보도만** 쓴다. 안내 문구는 "이면도로를 건너 17m 이동합니다. 횡단보도 표시가 없으니 차량을 살피세요." 로 나뉜다.
- 안양 실측(2026-09-07): 후보 16 → 간선 횡단 8 제외 · **채택 8**(현충로52번길·현충로48번길·경수대로1192번길·문예로24번길·안양로111번길). 안양문화원 → 소방서 정류장 374 → 75m. 실증 구간 9건 회귀 불변. 그래프 `anyang-hybrid-2026Q3r2`.

### 저상버스 우선 모드 (v1.22.0)

휠체어 이용자에게 일반버스는 탑승이 불가능하다. 그런데 종전 후보 스코어(도보거리 + 정거장 수 + 탑승 횟수)에는
저상 여부가 없어, 바로 앞 정류장의 저상 없는 마을버스가 조금 떨어진 정류장의 저상 노선보다 먼저 안내됐다
(안양시장애인복합문화관 → 안양시청, 10-1 대 6번). 저상 여부를 가중치가 아니라 **탑승 가능 여부**로 다룬다.

- `POST /route/plan` `low_floor`(bool). 생략하면 **휠체어 프로필은 on**, 그 외 프로필은 off. `mode` 가 `walk_bus`·`walk_bus_subway` 일 때만 의미가 있다.
- 1차 필터(정적): `tran_bus_route_info.low_bus_yn`(경기버스정보 저상버스 노선현황, 전일 기준 — 01 v1.5.0 / 08 `GBIS_LOWFLOOR`)이 `N` 인 노선은 후보에서 뺀다. 값이 없거나 기준일이 7일보다 오래되면 통과.
- 2차 판정(조회 시점 실시간): 승차 후보 **정류장 단위**로 도착정보를 한 번씩 조회해 노선별 다음 저상 차량을 본다. 승차 정류장 도착 전에 지나가는 차량은 뺀다(도보 소요 + 180초). 도착정보의 2대가 모두 일반 차량이면 그 노선만 위치정보로 상류의 저상 차량을 찾아 정거장 수로 대기를 추정한다(순환 노선 보정).
- 정렬은 계층 → 시간: **tier 1** 저상 도착 확인 / **tier 2** 운행 중 저상 추정 / **tier 0** 실시간 미확인 / **tier 3** 저상 없음. 같은 계층 안에서 도보 + 대기 + 정거장 수 + 승하차 오버헤드 + 하차 후 도보 시간으로 정렬하고, 상위 후보를 실제 휠체어 보행 경로로 다시 계산한 뒤 실제 도보 소요로 재판정한다.
- 반경: 출발·도착 인근 450m 에 탈 수 있는(tier 1·2) 저상 후보가 없을 때만 800m 로 넓힌다(`low_floor.expanded_radius`).
- 저상이 어디에도 없으면 404 가 아니라 정적 1순위 경로를 주고 `summary.warnings[0]` 에 "현재 운행 중인 저상버스가 없습니다" 를 둔다. 실시간을 못 받으면 tier 0 으로 정적 순서를 유지하고 "확인하지 못했습니다" 경고를 둔다.
- 응답: 최상위 `low_floor{mode, tier, expanded_radius, queried_at, valid_for_sec, wait_sec}`, 버스 leg `low_floor{tier, wait_sec, predict_min, stops_away, plate_no, source, reason}`. tier 1·2 면 `summary.duration_sec` 에 대기 시간이 더해지고 `eta_note` 가 그 사실을 말한다. 실시간 기반이므로 `valid_for_sec`(120초) 이 지나면 재탐색을 권한다.
- 호출량: 경로 탐색 1회당 승차 후보 정류장 수(보통 2~4)만큼 도착정보를, 필요한 노선만 위치정보를 호출한다. 20초 캐시가 같은 정류장 재호출을 흡수한다.

### 회전 안내 정비 — 방위각 평활·짧은 링크 흡수 (v1.21.0)

회전각을 링크 좌표열의 첫/마지막 두 점으로 재고 있었다. 인도 면형을 중심선화하면 교차로 모서리에
1~2m 짜리 미세 절점이 흔히 생기는데, 그 조각의 방위각은 실제 진행 방향과 무관하게 크게 튄다.
그 값으로 회전을 판정하니 평범한 모퉁이가 급좌회전·유턴으로 승격됐다(실증 2026-09-03, -137.7도).

- 방위각은 링크 진입·이탈 **10m 구간**으로 잰다 (`geo.lead_bearing` / `geo.trail_bearing`).
- **12m 미만 링크의 회전은 안내하지 않고 각을 다음으로 이월**한다. 이월각이 합산되므로 좌우로
  되꺾이는 지그재그는 서로 상쇄된다. "우회전 후 6m" 같은 지시가 사라진다.
- 특수 링크(횡단보도 등) 사이에 낀 **8m 미만 연결부는 앞 스텝에 흡수**한다.
- 유턴 억제 재탐색(`planner._uturn_edges`)도 같은 평활 방위각을 쓴다 — 멀쩡한 링크가 유턴으로
  오검출돼 페널티를 받고 오히려 우회가 나오던 것을 막는다.

### 횡단보도 안내 — 지시형에서 정보형으로 (v1.21.0)

노드 부착 횡단보도(`crossing_point`)는 **그 지점에 횡단보도가 있다**는 안내 전용 계층이고,
경로가 그것을 건넌다는 뜻이 아니다(실제 횡단은 `link_type='crossing'` 링크뿐이다).
그런데 종전에는 경로 중간 노드마다 "횡단보도를 건너세요"라고 지시했고, 안양아트센터→안양문화원
1.4km 경로에서 그 지시가 10번, 그중 6번은 같은 도로를 직진하는 도중에 나왔다. 듣는 사람은
지시대로 길 건너편으로 넘어가므로 안내가 경로 이탈을 만든다.

- 경로가 그 지점을 **직진 통과하면 알리지 않는다**. 시각장애 프로필만 차도 접근 신호로서 정보형 유지.
- 꺾이는 지점이면 정보형("횡단보도가 있는 지점입니다")으로만 알린다.
- 지시형은 실제 횡단 링크 스텝에서만 쓴다.

같은 경로에서 스텝 39개 → 23개, 지시형 발화 10회 → 0회로 줄었다.

### 통행 장애물 레이어 (v1.21.0)

수치지도 1:1,000 에서 계단·옹벽·담장·가드펜스·수목을 읽어 통행 판정과 링크 신설 게이트에 쓴다
(`topomap.obstacles.ObstacleIndex`). 안양 205매 실측: 계단 1,533 · 옹벽 9,160 · 담장/펜스 29,565 · 수목 25,152.

- `C0390000` 은 계단 전용 코드가 아니라 **계단·스탠드** 지물이다. 스탠드(경기장 관람석)를 걸러내지 않으면
  88m x 76m 폴리곤 한 장이 그 위를 지나는 보도 12개를 통째로 "계단 관통"으로 만든다 — 멀쩡한 보도를
  막는 반대 방향의 사고가 되므로 `구조='스탠드'` 는 제외한다.
- 계단 관통 길이는 폴리곤을 **먼저 합집합한 뒤** 잘라서 잰다. 교차 결과를 더하면 겹쳐 그린 계단 단수
  때문에 105m 링크에 관통 134m 가 나온다.
- `scripts/mark_stairs_from_topomap.py` 로 계단을 지나는 기존 링크를 감사하거나 `steps` 로 재분류한다.
  휠체어 프로필은 이미 `avoid=("steps", ...)` 로 계단을 회피하므로 라벨만 붙으면 통행 판정이 작동한다.

### 버스 구간 지도선 — 노선형상 (v1.20.0)

버스 leg 의 `geometry` 는 경유 정류장 좌표를 순서대로 이은 직선이라 지도에서 건물을 뚫고 지나갔다(실증 2026-09-03).
GBIS 노선형상(`getBusRouteLineListv2`, `DATA_GO_KR_API_KEY` 동일)을 받아 승차→하차 정류장 사이를 잘라 쓴다.
형상은 하루 캐시하고, 못 받거나 정류장이 형상에서 120m 보다 멀거나 순서가 뒤집히면(편도 형상) 종전 정류장 직선으로 돌아간다.
`legs[].geometry_source` 가 `gbis_line` / `stops` 로 출처를 말한다. `est_distance_m` 은 종전대로 정류장 직선 합이다.

### 경사 제약 — 권장·하드 2단계 (v1.20.0)

종전에는 프로필 `max_slope_deg`(수동휠체어 4도)가 통행 불가 하드 필터였다. 등고선 기반 5m DEM 은 짧은 링크에서
경사가 튀어(8m 횡단 링크에 7.8도) 실제로는 평탄한 205m 직행로가 잘리고 649m 우회가 나왔다(실증 2026-09-03, 안양문화원→안양세무서 정류장).

- `max_slope_deg` = **권장** 상한. 초과 링크는 비용을 가중(`slope_over_penalty` × 초과 도수)하고 경고를 붙인다 — 우회로가 있으면 피하고, 우회가 몇 배로 길어지면 지난다.
- `hard_slope_deg` = **통행 불가** 상한(수동 8도·전동 10도, 나머지는 권장과 동일). 제약 완화 폴백(+2·+4도)은 이 값 기준이다.
- **15m 미만 짧은 링크는 경사로 막지 않는다**(가중만). 안내 문구도 "짧은 구간 경사 추정 n도"로 추정임을 밝힌다.
- 점자블록 경고(`점자블록 없음`)는 **시각장애 프로필에서만** 붙는다. 휠체어 안내에 점자블록 안내가 섞이던 결함 수정.

### 역 편의시설 — 설비 단위 (v1.19.0)

`poi_station_access_status` 는 역별 **개수·유무**뿐이었다. 국가철도공단 파일(01 v1.3.0 테이블 3종
`poi_station_elevator_unit` / `poi_station_toilet_unit` / `poi_station_platform` + 기존 `poi_station_wheelchair_lift`)을
붙여 `GET /transit/station/facilities?name=범계역` 이 **어느 출입구의 엘리베이터인지, 장애인화장실이 게이트 안인지 밖인지,
승강장에 안전발판이 있는지, 열차와의 이격거리가 몇 cm 인지**까지 답한다. 지하철 leg 의 `board`/`alight` 에도 요약(`facilities`)이 붙는다.

유무 필드는 **3상태**(`yes` / `no` / `unknown`)다. 코레일 편의시설 API 는 안양역만 응답하고 나머지 6역은 `NULL` 이라,
`unknown` 을 "없음"으로 말하면 틀린다. 실시간 승강기 가동 여부는 코레일이 제공하지 않아 싣지 않는다.

### 접근성 판정은 3상태다

`accessible` 은 `true` / `false` / `null` 셋을 가지며, **`null` 은 "접근 불가"가 아니라
"판정 근거 없음"** 이다. 역은 승강설비 데이터로 판정하지만 정류장은 근거가 없어 `null` 이다.
소비 측이 falsy 로 뭉뚱그려 "이용 불가"로 표시하는 사고를 막기 위해 판정 상태를 별도 필드로
같이 준다 — `accessible_status` 는 `"yes"` / `"no"` / `"unknown"` 문자열이다.

`unknown` 인 정류장은 불가로 표시하지 말고, `warnings` 의 안내대로 실시간 도착정보를
확인하도록 유도해야 한다. 노선의 저상버스 운영 여부(`tran_bus_route_info.low_bus_yn`)를
확보하면 `unknown` 을 좁힐 수 있다.

### 정류장 경유 노선 (`routes`)

노선번호만으로는 노선이 특정되지 않는다 — 안양 연관 117개 노선 중 **번호가 겹치는 것이
12쌍**이다. 번호 `2` 는 일반형시내버스 `213000017` 과 마을버스 `241253001` 둘 다이고,
김중업건축박물관을 지나는 것은 후자뿐이다. 번호만 안내하면 다른 버스에 타게 된다.

```json
"routes": [
  {"route_id": 241253001, "name": "2", "type": "마을버스",
   "end_station": "안양역", "station_seq": [10, 34]}
]
```

- `end_station` — 종점명. 이용자가 정류장 안내판에서 그대로 대조할 수 있는 방면 정보다.
- `station_seq` — 그 노선이 이 정류장을 지나는 순번. 회차 노선은 한 정류장을 두 번 지나므로
  값이 여럿일 수 있다. 승차·하차 정류장의 순번을 비교하면 진행 방향을 판정할 수 있어,
  이름이 같은 양방향 정류장(예: 안양역 `09213`/`09145`)에서 반대편 차를 타는 것을 막는다.

순번 비교에는 한계가 있다. 순환 노선은 승차 순번이 하차 순번보다 클 수 있고(종점을 지나
계속 운행), 양쪽 값이 여럿이면 조합이 여러 개 나온다. 배열끼리 직접 비교하지 말고 조합을
검토하되, 확정이 어려우면 `end_station` 을 함께 안내해 현장에서 대조하게 해야 한다.

## 데이터

| 구분 | 현재 | 비고 |
|---|---|---|
| 보행 네트워크 | **OSM 안양 보행망** — 노드 6,750 / 링크 9,712 | 원본(node/link) 수령 시 `--source tabular` 로 재구축 후 `/admin/reload-network` — API 스키마 불변 |
| 경사 | **5m DEM** — 1:5,000 수치지형도 등고선(주곡선 5m)을 보간해 생성 (`scripts/dem_from_contours.py`) | 공개DEM 90m(`.img`)도 그대로 사용 가능. DEM 이 아예 없으면 `--elevation terrain`(공개 지형 타일, 인증 불필요) |
| 무장애 관광지 | 01-IITP-DABT-Database `mv_poi` 정본 (`POI_BACKEND=db`) — 한국어 시설 문구를 `*_yn` 체계로 정규화(`MVPOI_FACILITY_MAP`), **안양 31건 실측** | 08 파이프라인이 수집·적재. 적재 전에는 `file`/`none` 백엔드로 기동 가능 |
| 역·정류장 | `poi_station_access_status` 412역(안양 7역) + `tran_bus_station_info` 3,496 + 설비 단위 3종(v1.19.0) | 실시간 저상 여부는 GBIS 도착·위치 API |

### 안양 그래프 실측 (v1.3.0)

노드 6,750 / 링크 9,712 — 도로 71.3% · 보도 20.8% · 횡단보도 4.3% · 계단 50 · 육교 44 · 지하보도 35

| 경사 격자 | 4° 초과 링크 | 안양역 → 안양예술공원 (수동 휠체어) |
|---|---|---|
| 공개DEM 90m | 1,087개 (11.2%) | 2,390m (일반 보행 대비 **+5m**) |
| **수치지형도 5m** | 965개 (9.9%) | 2,608m (일반 보행 대비 **+220m**) |

90m 격자는 언덕의 경사를 인접 평지 도로에까지 번지게 하면서, 정작 짧은 급경사 구간은
평활화해 놓친다. 5m 로 바꾸자 **실제로 피해야 할 오르막이 드러나 +220m 우회가 발생**한다.
경사 데이터의 해상도가 곧 안내의 정확도다.

보도폭·턱낮춤은 OSM 태그 커버리지가 0.1%/0% 라 현재 판정에 거의 기여하지 못한다 —
융기원 원본 데이터(WIDTH·CURB)에서 보강해야 하는 항목이다.

데이터 품질은 `/meta/network` 와 경로 응답의 `data_quality` 로 항상 노출한다. 계단·경사 속성이
없는 네트워크에서는 회피 판정이 성립하지 않으므로 반드시 확인할 것.

## 테스트

```bash
python -m pytest -q
```

## 라이선스

이 프로젝트는 MIT 라이선스로 배포됩니다. 전문은 [LICENSE](LICENSE) 파일을 참고하십시오.

본 연구는 정부(과학기술정보통신부)의 재원으로 정보통신기획평가원의 지원을 받아 수행된 연구입니다.
(연구개발과제번호 RS-2024-003976, 데이터 기반 장애인 데이터 탐색·활용 해결기술 개발)
