FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.api.txt requirements.txt
RUN pip install --no-cache-dir --timeout 300 -r requirements.txt
COPY app/ ./app/
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
