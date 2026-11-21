FROM python:3.12-slim

# Tesseract (OCR engine + English and orientation data). Add e.g. tesseract-ocr-ben for Bangla and set OCR_LANG=eng+ben.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# run as an unprivileged user
RUN useradd --create-home --uid 10001 app && mkdir -p /data/uploads && chown -R app /data /srv
COPY --chown=app app ./app
USER app

EXPOSE 8000
# default = API; the worker service overrides the command (see docker-compose.yml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
