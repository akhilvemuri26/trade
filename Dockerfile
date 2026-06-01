FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements.txt
COPY executor/requirements.txt executor/requirements.txt
RUN pip install --no-cache-dir \
    -r requirements.txt \
    -r executor/requirements.txt

COPY . .

# Persistent data mount point (Fly volume → /data)
RUN mkdir -p /data/results/logs /data/positions

ENV DATA_DIR=/data
ENV PLAIN_LOGS=true

EXPOSE 8080

CMD ["python", "run_server.py"]
