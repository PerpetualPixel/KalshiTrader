FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY kalshitrader ./kalshitrader
RUN pip install --no-cache-dir ".[ai]"

RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8000

# Default: paper-trade. Override with `docker run ... kalshitrader dashboard --host 0.0.0.0`.
CMD ["kalshitrader", "run"]
