FROM python:3.11-slim

# Системные зависимости для web3/cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ .

# /data — persistent volume (логи, pair_addresses.json)
RUN mkdir -p /data

CMD ["python", "main.py", "--triangle"]
