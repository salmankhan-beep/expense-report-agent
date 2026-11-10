FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /bin/

WORKDIR /app

# requirements.txt is a hash-pinned export of the lockfile:
#   uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt
# Re-export whenever dependencies change.
COPY requirements.txt ./
RUN uv pip install --system --no-cache --require-hashes \
    --index-url https://pypi.org/simple -r requirements.txt

COPY main.py ./
COPY static ./static

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
