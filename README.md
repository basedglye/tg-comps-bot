# tg-comps-bot
[![Deploy on Railway](https://railway.app/button.svg)](
  https://railway.com/project/6649ea7a-46ef-407d-9664-258ec77e0402/service/96380721-c3bd-45b1-8a02-734e28361b81?environmentId=1bc763c5-274b-4534-af58-45d45ce26138
)



## 🚑 Build Troubleshooting (Railway)

**Error:** `Package 'pango-graphite' has no installation candidate … exit code 100`

**Cause:** The Debian Bookworm base image no longer provides `pango-graphite`.  
**Fix:** Use the correct WeasyPrint deps and remove `pango-graphite`.

Use this Dockerfile:

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 libffi-dev libxml2 libxslt1.1 libjpeg62-turbo zlib1g \
    fonts-dejavu-core fonts-dejavu-extra && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8000
CMD ["python","app.py"]
