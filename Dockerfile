FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY scanner ./scanner
RUN pip install --no-cache-dir .

# The diary lives on a persistent volume mounted here by the host. (No VOLUME
# instruction: Railway rejects it and mounts its own volume at this path.)
RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "-m", "scanner"]
