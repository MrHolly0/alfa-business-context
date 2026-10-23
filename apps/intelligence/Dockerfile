FROM python:3.12.14-slim AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY apps/intelligence/intelligence ./intelligence
RUN mkdir -p /app/.state \
    && chown -R 10001:10001 /app
USER 10001:10001

FROM runtime-base AS runtime
ENTRYPOINT ["python", "-m", "intelligence.llm"]
CMD ["demo"]

FROM runtime-base AS test
COPY apps/intelligence/tests ./tests
USER 10001:10001
ENTRYPOINT ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
