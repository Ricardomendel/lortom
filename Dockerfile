FROM python:3.9-slim

# portaudio19-dev + gcc are needed to build PyAudio (listed in requirements.txt
# but unused by app.py); build-essential covers gcc/make for any other native
# extensions (e.g. greenlet) that don't ship a wheel for this platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT at runtime; gunicorn must bind to it.
CMD ["sh", "-c", "gunicorn -k eventlet -w 1 -b 0.0.0.0:$PORT app:app"]
