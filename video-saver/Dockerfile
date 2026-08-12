# Образ тонкий: из системного здесь нужен только ffmpeg.
FROM python:3.12-slim

# ffmpeg обязателен. YouTube и подобные площадки отдают видео и звук разными
# потоками — без склейки получится либо немое видео, либо только звук. Он же
# достаёт mp3, когда просят «только звук».
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY vsaver ./vsaver
RUN pip install --no-cache-dir .

# Скачанные файлы и база задач живут здесь — том монтируется снаружи.
VOLUME ["/data"]
EXPOSE 8080

# Сервис ничего не пишет за пределами /data, поэтому запускаем не от root:
# дыра в разборе чужой ссылки не должна давать доступ ко всему контейнеру.
RUN useradd --system --uid 10001 --home /app vsaver \
 && mkdir -p /data && chown vsaver:vsaver /data
USER vsaver

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/config', timeout=4)"

ENTRYPOINT ["python", "-m", "vsaver"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--data", "/data"]
