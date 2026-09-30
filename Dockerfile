FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WANREN_DB_PATH=/app/data/wanren.sqlite3 \
    WANREN_STORAGE_PATH=/app/storage

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY wanren ./wanren
COPY static ./static
COPY scripts ./scripts

RUN groupadd --gid 1000 wanren \
    && useradd --uid 1000 --gid wanren --create-home wanren \
    && mkdir -p /app/data /app/storage \
    && chown -R wanren:wanren /app

USER wanren
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "wanren.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
