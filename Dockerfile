FROM python:3.9-slim

# Without this, Python fully buffers stdout when it isn't a terminal (as in a
# container), so print()/logger output can sit unflushed and never reach
# Render's log stream -- exactly the kind of "missing" log that makes a
# production-only bug impossible to diagnose.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT at runtime; gunicorn must bind to it.
CMD ["sh", "-c", "gunicorn -k eventlet -w 1 -b 0.0.0.0:$PORT app:app"]
