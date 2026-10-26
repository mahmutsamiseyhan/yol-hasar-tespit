FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# OpenCV/ultralytics çalışma zamanı bağımlılıkları
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU sürümü PyTorch: imaj ~1.5 GB küçülür; GPU sunucuda bu satırı kaldırın
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY data/bursa_context.geojson ./data/bursa_context.geojson
# KVKK maskeleme modelleri imaja gömülür: demo ortamında internet gerekmez
RUN python scripts/download_models.py

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
# Ters vekil (Caddy) arkasında gerçek istemci IP'si X-Forwarded-For başlığından alınır (istek sınırı için)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
