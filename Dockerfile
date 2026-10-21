# --- web UI ----------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- service ---------------------------------------------------------------------------
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# The built UI, served by Flask from web/dist (same origin as the API and the session).
COPY --from=web /web/dist ./web/dist

ENV PORT=8080
ENV DEBUG=false

EXPOSE 8080

# Runs as a non-root user: nothing here writes to the filesystem in-cluster (Postgres holds
# all state), so the process needs no privileges at all.
RUN useradd --system --uid 10001 shorthop
USER 10001

CMD ["python", "main.py"]
