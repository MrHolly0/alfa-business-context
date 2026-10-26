FROM python:3.12.14-slim AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY apps/intelligence/requirements.lock /tmp/requirements.lock
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY apps/intelligence/intelligence ./intelligence
COPY apps/intelligence/examples ./examples
COPY apps/intelligence/evaluation ./evaluation
RUN mkdir -p /app/.state && chown -R 10001:10001 /app
USER 10001:10001

FROM runtime-base AS runtime
ENTRYPOINT ["python", "-m", "intelligence"]
CMD ["demo"]

FROM runtime-base AS test
USER root
COPY apps/intelligence/requirements-dev.lock /tmp/requirements-dev.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements-dev.lock
COPY apps/intelligence/tests ./tests
USER 10001:10001
ENTRYPOINT ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
