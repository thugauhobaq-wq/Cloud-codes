FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git — рабочий инструмент этого бота, а не удобство разработчика: без него
# нечем ни закоммитить сгенерированный проект, ни запушить его.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# botkit лежит рядом и ставится из исходников. Сборку запускайте из каталога,
# который содержит и botkit, и этот проект:
#     docker build -f botfactory/Dockerfile -t botfactory .
COPY botkit /app/botkit
RUN pip install --no-cache-dir /app/botkit

COPY botfactory/pyproject.toml botfactory/README.md ./
COPY botfactory/src ./src
RUN pip install --no-cache-dir .

# Понижает привилегии entrypoint: сначала он выравнивает права на
# смонтированном /app/data, до которого правам из образа не достать.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data/projects \
    && chown -R bot:bot /app
USER bot

# Автор коммитов: без него git в контейнере отказывается коммитить.
RUN git config --global user.name "botfactory" \
    && git config --global user.email "botfactory@localhost" \
    && git config --global --add safe.directory '*'

COPY botfactory/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER root
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENV APP_USER=bot
ENTRYPOINT ["docker-entrypoint.sh"]

VOLUME ["/app/data"]

ENV DB_PATH=/app/data/botfactory.db \
    WORKDIR=/app/data/projects

CMD ["python", "-m", "botfactory", "run"]
