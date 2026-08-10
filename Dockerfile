FROM python:3.11-slim

WORKDIR /app

# DejaVu covers Latin; Amiri/Noto give proper Arabic glyphs for story captions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-hosny-amiri \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py paymob_client.py kie_client.py gunicorn_config.py ./
COPY templates/ templates/
COPY static/ static/
COPY .env.example .env.example

ENV COLORING_DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]
