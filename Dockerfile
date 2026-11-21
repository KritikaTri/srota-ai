FROM python:3.11-slim

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source and use the committed seed DB as the runtime DB
COPY . .
RUN cp srotaai_seed.db srotaai.db \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD uvicorn srotaai.web.app:app --host 0.0.0.0 --port $PORT
