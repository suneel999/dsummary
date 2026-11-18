FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

WORKDIR /app

# Metric-compatible fonts (Times New Roman/Arial/Calibri substitutes) so the report
# generator can measure text the same way Word lays it out, and LibreOffice (headless)
# to produce the PDF preview/download on Linux.
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-liberation fonts-crosextra-carlito \
        fonts-crosextra-caladea fonts-wine fonts-dejavu-core libreoffice-writer-nogui && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "gunicorn --workers ${GUNICORN_WORKERS:-3} --bind 0.0.0.0:${PORT:-8000} --timeout ${GUNICORN_TIMEOUT:-180} main:app"]
