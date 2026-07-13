# 02-IITP-DABT-Route — 무장애 보행 경로 추천 API 서비스 이미지
#
# 그래프 구축(osmnx/rasterio/geopandas)은 이 이미지에 포함하지 않는다.
# 구축은 오프라인 작업이고, 서비스는 완성된 그래프(.gpickle)와 건물 폴리곤(.pkl)만 읽는다.
# 데이터는 볼륨으로 주입한다(이미지 재빌드 없이 그래프 교체 가능).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY route_service/ ./route_service/
COPY scripts/ ./scripts/

EXPOSE 18100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18100/health || exit 1

CMD ["uvicorn", "route_service.api.main:app", "--host", "0.0.0.0", "--port", "18100"]
