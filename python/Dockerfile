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
RUN uv sync --frozen --no-dev
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "maat.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
