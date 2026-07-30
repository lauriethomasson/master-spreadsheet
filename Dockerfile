FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default so a plain local `docker run` (no -e PORT=...) doesn't crash with
# a confusing empty --server.port=. Cloud Run always sets PORT itself at
# deploy time, which overrides this.
ENV PORT=8080

# Headless is required, not optional, in a container: without it Streamlit's
# first-run "usage statistics" prompt waits on stdin for an answer that will
# never come (no TTY), and the container just hangs instead of serving.
CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
