# Maat Python image — serves three roles selected by compose `command`:
#   reader (default CMD), the extract+classify agents, and the acquisition-clock tick.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY maat ./maat
# scripts/ holds the acquisition clock (clock.py) and one-off acquire.py the clock service runs;
# bundle them so the same image can drive ingestion, not just serve.
COPY scripts ./scripts
# --extra obs ships the OpenTelemetry exporter so the pipeline emits LLM spans (token usage =
# spend) to cat-cafe when OTEL_EXPORTER_OTLP_ENDPOINT is set. No-op without the endpoint.
RUN uv sync --frozen --no-dev --extra obs
# Drop root: run as a non-privileged app user (semgrep missing-user, #281). All roles this image
# drives (reader on :8000, agents, clock) need no write access beyond /app and no privileged port.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "maat.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
