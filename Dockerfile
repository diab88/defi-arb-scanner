FROM python:3.12-slim

# Flush logs immediately (nicer container logs)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (single self-contained image — no bind mounts needed)
COPY scanner.py dashboard.py monitor.py backtest.py ./

# Persisted state (portfolio + notifications). Mount a volume here to keep it across
# restarts; on ephemeral platforms it simply resets.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Divio (and most container PaaS) route HTTP to port 80.
EXPOSE 80

# Bind to 0.0.0.0 and honor a platform-injected $PORT, defaulting to 80 (Divio's
# requirement). Locally, docker-compose maps host 8765 -> container 80.
CMD ["sh", "-c", "python dashboard.py --host 0.0.0.0 --port ${PORT:-80}"]
