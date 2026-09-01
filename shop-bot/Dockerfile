FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельным слоем: правка кода не заставляет ставить их заново.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Процесс не должен ходить под root — ему нужен только каталог с базой.
# Понижает привилегии entrypoint: сначала он выравнивает права на
# смонтированном /app/data, до которого правам из образа не достать.
RUN useradd --create-home --uid 1000 shop \
    && mkdir -p /app/data \
    && chown -R shop:shop /app

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENV APP_USER=shop
ENTRYPOINT ["docker-entrypoint.sh"]

VOLUME ["/app/data"]

ENV DB_PATH=/app/data/shop.db

CMD ["python", "-m", "shop", "run"]
