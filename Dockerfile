FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh
RUN useradd --create-home appuser \
    && mkdir -p /app/private_media /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "cms.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
