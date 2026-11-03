FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# cairosvg draws the card's share images; it needs cairo and one real font.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcairo2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY scanner ./scanner
RUN pip install --no-cache-dir .

# The diary lives on a persistent volume mounted here by the host. (No VOLUME
# instruction: Railway rejects it and mounts its own volume at this path.)
RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "-m", "scanner"]
