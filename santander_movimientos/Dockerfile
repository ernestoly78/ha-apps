# Imagen Debian (glibc): Playwright/Chromium no corre sobre Alpine.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SCRAPER_HEADLESS_MODE=true \
    DEBUG=false

RUN pip install --no-cache-dir fintself==1.5.0 "paho-mqtt>=2,<3" requests tzdata \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache

COPY run.py /app/run.py
WORKDIR /app
CMD ["python", "-u", "/app/run.py"]
