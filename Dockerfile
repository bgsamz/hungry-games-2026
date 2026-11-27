FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /app
RUN pip install --no-cache-dir Pillow==11.3.0
COPY server.py ./
COPY static ./static

# Runs as root so the ./data folder mounted from the host is always writable,
# whatever user created it there.
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/api/ratings', timeout=2)"

CMD ["python", "server.py"]
