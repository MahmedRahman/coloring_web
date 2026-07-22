FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py paymob_client.py gunicorn_config.py ./
COPY templates/ templates/
COPY .env.example .env.example

ENV COLORING_DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]
