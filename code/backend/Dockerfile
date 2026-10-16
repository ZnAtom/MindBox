ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY app ./app
COPY knowledge ./knowledge
RUN useradd --create-home worker && mkdir -p /app/var && chown -R worker:worker /app/var
USER worker
CMD ["python", "-m", "app.cli", "--help"]
