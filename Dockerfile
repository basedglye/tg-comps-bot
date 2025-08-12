FROM python:3.11-slim

# system deps for WeasyPrint
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libcairo2 pango-graphite libpango-1.0-0 libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 libffi-dev fonts-dejavu-core fonts-dejavu-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8000
CMD ["python","app.py"]