FROM python:3.12-slim

# Зависимостей нет вообще: ни pip install, ни сборки фронтенда.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY widget ./widget
COPY README.md ./

# Процесс не должен ходить под root — ему нужен только каталог с базой.
RUN useradd --create-home --uid 1000 widget \
    && mkdir -p /app/data \
    && chown -R widget:widget /app
USER widget

VOLUME ["/app/data"]
EXPOSE 8080

# Настройки читает сам CLI, поэтому entrypoint-скрипт не нужен. Внутри
# контейнера слушаем все интерфейсы: наружу порт пробрасывает docker, а перед
# ним стоит nginx с сертификатом.
ENV WIDGET_DB=/app/data/widget.db \
    WIDGET_HOST=0.0.0.0 \
    WIDGET_PORT=8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ['WIDGET_PORT'] \
+ '/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "widget", "serve"]
