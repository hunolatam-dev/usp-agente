FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Usuario sin privilegios
RUN useradd --create-home agente && chown -R agente /app
USER agente

EXPOSE 8000
# Forma shell para que ${PORT} (lo asigna Railway) se expanda al arrancar
CMD uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"
