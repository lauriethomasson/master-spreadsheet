# Microsoft's own official Playwright Python image already bundles
# Chromium plus every system library (fonts, libnss3, libgbm, libatk, ...)
# it needs to actually run headless - the whole reason this service is
# built on it rather than hand-rolling those apt-get installs on top of a
# plain python:slim base (which is exactly the kind of "substantial
# infrastructure change" the main app's own Dockerfile deliberately never
# takes on - see this repo's README for why Chromium never runs in that
# container at all).
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8080
# Two concurrent renders by default - see app.py's own MAX_CONCURRENT_
# RENDERS docstring. Override per-deployment if the service's own Cloud
# Run memory/CPU allocation can safely support more.
ENV MAX_CONCURRENT_RENDERS=2

CMD ["python", "app.py"]
