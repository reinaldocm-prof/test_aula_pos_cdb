FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py .

# Mostra os prints imediatamente no "docker compose logs".
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
