FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# botkit лежит рядом и ставится из исходников. Сборку запускайте из каталога,
# который содержит и botkit, и этот проект:
#     docker build -f giveaway-bot/Dockerfile -t giveaway-bot .
COPY botkit /app/botkit
RUN pip install --no-cache-dir /app/botkit

# Зависимости отдельным слоем: правка кода не заставляет ставить их заново.
COPY giveaway-bot/pyproject.toml giveaway-bot/README.md ./
COPY giveaway-bot/src ./src
RUN pip install --no-cache-dir .

# Процесс не должен ходить под root — ему нужен только каталог с базой.
# Понижает привилегии entrypoint: сначала он выравнивает права на
# смонтированном /app/data, до которого правам из образа не достать.
RUN useradd --create-home --uid 1000 giveaway \
    && mkdir -p /app/data \
    && chown -R giveaway:giveaway /app

COPY giveaway-bot/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENV APP_USER=giveaway
ENTRYPOINT ["docker-entrypoint.sh"]

VOLUME ["/app/data"]

ENV DB_PATH=/app/data/giveaway.db

CMD ["python", "-m", "giveaway", "run"]
