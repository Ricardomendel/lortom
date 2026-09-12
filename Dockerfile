FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT at runtime; gunicorn must bind to it.
CMD ["sh", "-c", "gunicorn -k eventlet -w 1 -b 0.0.0.0:$PORT app:app"]
