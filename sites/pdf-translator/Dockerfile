# Образ намеренно тонкий: у приложения нет зависимостей, кроме самого Python
# и шрифтов. Всё остальное — стандартная библиотека.
FROM python:3.12-slim

# fonts-dejavu-core — латиница, кириллица и греческий, этого хватает для
# большинства пар языков. fonts-noto-core добавляет иврит, арабицу и деванагари.
# Иероглифы (fonts-noto-cjk) весят под сотню мегабайт, поэтому ставятся только
# при сборке с --build-arg CJK=1.
ARG CJK=0
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-noto-core \
 && if [ "$CJK" = "1" ]; then apt-get install -y --no-install-recommends fonts-noto-cjk; fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pdftr ./pdftr
RUN pip install --no-cache-dir .

# Задачи, исходные файлы и кэш переводов живут здесь — том монтируется снаружи.
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/config', timeout=4)"

ENTRYPOINT ["python", "-m", "pdftr"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--data", "/data"]
