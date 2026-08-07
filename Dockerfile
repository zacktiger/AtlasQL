# The API only. The ETL jobs are not in here on purpose: they run once against
# a database, they need a GDAL/GEOS/PROJ stack an order of magnitude larger than
# the app, and they download gigabytes of source archives. Building the database
# is a job you run from a checkout; serving it is what gets deployed.
FROM python:3.13-slim

# psycopg[binary] ships its own libpq, so there is nothing to apt-get here.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements-serve.txt .
RUN pip install --no-cache-dir -r requirements-serve.txt

COPY atlasql/ ./atlasql/
COPY frontend/ ./frontend/
COPY sql/ ./sql/

# Run unprivileged. Nothing here is written to at runtime.
RUN useradd --create-home --uid 10001 atlasql && chown -R atlasql:atlasql /app
USER atlasql

# Hosts inject the port to listen on; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

# 0.0.0.0, not `::`. Binding the IPv6 wildcard gets an IPv6-only socket here,
# and every published IPv4 connection is then refused — which looks exactly like
# a crashed app, except the logs say the server started fine. Render, Railway
# and Fly all reach a container over IPv4.
CMD ["sh", "-c", "exec uvicorn atlasql.api:app --host 0.0.0.0 --port ${PORT}"]
