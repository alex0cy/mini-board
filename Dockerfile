FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

# БД лежит в примонтированном томе, а не в образе — образ остаётся stateless
ENV MINIBOARD_DB=/data/board.db
VOLUME ["/data"]
EXPOSE 8088

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/healthz',timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
