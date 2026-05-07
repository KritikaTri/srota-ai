FROM python:3.11-slim

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Seed the signal database at build time
RUN python -m srotaai.signals pv-india-otc --db srotaai.db --min-n 2

ENV PORT=8000
EXPOSE 8000

CMD uvicorn srotaai.web.app:app --host 0.0.0.0 --port $PORT
