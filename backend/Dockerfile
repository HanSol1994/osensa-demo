# The kitchen is a pure MQTT *client*: it opens an outbound connection and never
# listens. So this image exposes no ports and needs no web server.
FROM python:3.13-slim

# PYTHONUNBUFFERED so logs reach the platform's collector immediately instead of
# sitting in a buffer, which makes a crash look silent.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first so the install layer is cached across source edits.
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Run unprivileged. Nothing here needs root, and a container that does not need it
# should not have it.
RUN useradd --create-home --uid 10001 kitchen
USER kitchen

# All configuration comes from the environment (see backend/.env.example), so the
# same image runs against a local broker or a managed one with no rebuild.
CMD ["python", "-m", "restaurant"]
