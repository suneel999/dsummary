FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "gunicorn --workers ${GUNICORN_WORKERS:-3} --bind 0.0.0.0:${PORT:-8000} --timeout ${GUNICORN_TIMEOUT:-180} main:app"]
