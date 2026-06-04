FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY imap2mcp ./imap2mcp

ENV HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8000 \
    DB_PATH=/data/index.db

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "imap2mcp"]
