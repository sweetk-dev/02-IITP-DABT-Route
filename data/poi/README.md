# POI 데이터 (file 백엔드)

`POI_BACKEND=file` 일 때 이 디렉터리의 JSON 을 읽는다. 운영에서는 `POI_BACKEND=db` 로
01-IITP-DABT-Database 를 직접 조회한다(이동편의 데이터 파이프라인 적재 후).

> ⚠️ 실데이터는 이 레포에 커밋하지 않는다. 아래는 스키마 예시다.

## tour_bf.json — 무장애 관광지 (01: `poi_tour_bf_facility`)

```json
[
  {
    "poi_id": "TBF-0001",
    "name": "예시 관광지",
    "addr": "경기도 안양시 만안구 ...",
    "latitude": 37.3943,
    "longitude": 126.9568,
    "dis_toilet_yn": "Y",
    "elevator_yn": "Y",
    "dis_parking_yn": "Y",
    "slope_yn": "Y",
    "wheelchair_rent_yn": "N",
    "tactile_map_yn": "N",
    "audio_guide_yn": "N",
    "nursing_room_yn": "Y",
    "accessible_room_yn": "N",
    "stroller_rent_yn": "N",
    "entrance": {"lat": 37.3944, "lng": 126.9569}
  }
]
```

`entrance` 는 **무장애 출입구** 좌표다. 없으면 시설 대표 좌표로 대체되며
응답의 `resolved_by` 가 `facility_centroid` 로 표시된다(건물 중심으로 안내되므로 주의).

## stations.json — 지하철역 (01: `poi_station_access_status`)

```json
[{"poi_id": "1004", "name": "평촌역", "latitude": 37.3944, "longitude": 126.9628,
  "elevator_cnt": 2, "wheelchair_lift_cnt": 0, "dis_toilet_yn": "Y"}]
```

## transit_stops.json — 버스 정류장 (GBIS)

```json
[{"poi_id": "228000123", "name": "평촌역", "lat": 37.3945, "lng": 126.9631,
  "routes": ["11-5", "5624"]}]
```

저상버스 정차 여부는 정적 데이터에 없다. 실시간 도착정보(GBIS `lowPlate`)로 확인한다.
