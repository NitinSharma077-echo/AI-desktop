# Two stages, because the app needs two toolchains and only one of them at
# runtime: Node builds the React UI into static files, then the Python image
# serves those files alongside the API. Node never ships to production.
#
# This is also why the deploy is Docker rather than Render's native Python
# runtime -- that image is not guaranteed to have Node, and finding out during a
# deploy costs a build cycle.

# ---- stage 1: build the UI ------------------------------------------------
FROM node:24-slim AS ui

WORKDIR /ui

# Manifests first, so the dependency layer is cached and only reinstalls when
# they actually change -- editing a component does not re-run npm ci.
COPY frontend/my-react-app/package.json frontend/my-react-app/package-lock.json ./
RUN npm ci

COPY frontend/my-react-app/ ./
RUN npm run build


# ---- stage 2: the app -----------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Same caching reason as above: requirements change far less often than code.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# The built UI, from stage 1. main.py looks for exactly this path and falls back
# to serving the API alone if it is missing.
COPY --from=ui /ui/dist ./frontend/my-react-app/dist

# Render supplies $PORT at runtime and it is not known at build time, so this is
# the shell form on purpose -- the exec form would pass "$PORT" as a literal.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
