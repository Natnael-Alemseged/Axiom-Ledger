# Document Intelligence Refinery - minimal runtime image
FROM python:3.10-slim

WORKDIR /app

# Install system deps if needed (e.g. for sentence-transformers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Prefer uv for install (optional: use pip if uv not available)
COPY pyproject.toml uv.lock* ./
COPY src ./src
COPY configs ./configs
COPY rubric ./rubric

RUN pip install --no-cache-dir -e .

# Runtime dirs (refinery writes here)
ENV REFINERY_HOME=/app/.refinery
RUN mkdir -p /app/.refinery/profiles /app/.refinery/extractions /app/.refinery/pageindex /app/.refinery/vector_store

# Env vars (set at run time)
# OPENROUTER_API_KEY - for Vision/LLM and PageIndex summaries
# REFINERY_CHROMA_DIR - optional, default .refinery/vector_store
# REFINERY_FACT_DB - optional, default .refinery/fact_store.db

ENTRYPOINT ["python", "-m", "refinery"]
CMD []
