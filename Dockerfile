FROM python:3.11-slim

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Use the committed seed DB as the runtime DB
RUN cp srotaai_seed.db srotaai.db

ENV PORT=8000
EXPOSE 8000

CMD uvicorn srotaai.web.app:app --host 0.0.0.0 --port $PORT
