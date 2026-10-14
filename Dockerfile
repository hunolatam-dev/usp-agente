FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY usp-agente/requirements.txt .
RUN pip install -r requirements.txt

COPY usp-agente/ .

RUN useradd --create-home agente && chown -R agente /app
USER agente

EXPOSE 8000
CMD uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"
