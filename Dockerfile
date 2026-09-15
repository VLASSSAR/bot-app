FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --create-home --uid 10001 botuser
COPY pyproject.toml README.md ./
COPY vacancy_bot ./vacancy_bot
RUN pip install --no-cache-dir . && mkdir -p /app/data && chown -R botuser:botuser /app/data
USER botuser
CMD ["python", "-m", "vacancy_bot"]
