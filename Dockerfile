# This image is also the licence boundary: `formulas` is EUPL-1.1+ and lives only here.
# LICENCE.EUPL-1.2.txt and NOTICE ship inside the image so any copy of it carries the notices EUPL Article 5 requires.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Service code. evaluate.py is the only module that imports the engine.
COPY app.py evaluate.py runner.py ./
# Licence + attribution travel with the image (EUPL 1.2 Art. 5, Attribution Right).
COPY LICENCE.EUPL-1.2.txt NOTICE ./

# Run non-root: this process evaluates user-supplied formula logic.
RUN useradd --create-home --uid 10001 evaluator
USER evaluator

EXPOSE 8080
# Each request already forks a child for the evaluation itself (runner.py), so the
# server stays responsive; workers scale with instance count.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
