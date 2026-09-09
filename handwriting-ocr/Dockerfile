# Образ намеренно тонкий: у приложения нет зависимостей, кроме клиента
# Anthropic, а для движка Яндекса не нужен и он.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY hwocr ./hwocr
RUN pip install --no-cache-dir ".[claude]"

# Задачи и фотографии живут здесь — том монтируется снаружи.
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/config', timeout=4)"

ENTRYPOINT ["python", "-m", "hwocr"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--data", "/data"]
