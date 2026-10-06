FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data ./data
COPY evals ./evals
RUN useradd --system --uid 10001 app && chown -R app /srv
USER app
ENV DATA_DIR=/srv/data
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status == 200 else 1)"
# One process: the payout worker and the per-process PaySwift rate limiter assume a single instance.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "15"]
