FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY processador.py .

# Mostra os prints imediatamente no "docker compose logs".
ENV PYTHONUNBUFFERED=1

CMD ["python", "processador.py"]
