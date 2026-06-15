FROM python:3.12-slim
WORKDIR /app
COPY services/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/app/ .
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
