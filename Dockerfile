# ==========================================
# STAGE 1: Build the Frontend
# ==========================================
FROM node:20-alpine AS frontend-builder
WORKDIR /build

# Copy frontend dependency files first to leverage Docker caching
COPY frontend/package*.json ./
RUN npm ci

# Copy the rest of the frontend source code and build it
COPY frontend/ ./
RUN npm run build 
# (Note: Ensure your frontend build script outputs to 'frontend/dist')

# ==========================================
# STAGE 2: Build the Final Production Image
# ==========================================
FROM python:3.11-slim AS runner

# Install uv for lightning-fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# Copy Python dependency files
COPY pyproject.toml ./

# Install dependencies globally in the container (no need for a virtualenv inside Docker)
RUN uv pip install --system -r ./pyproject.toml

# Copy only the folders required for the backend
COPY app/ ./app/
COPY db_manifests/ ./db_manifests/
COPY kb/ ./kb/
COPY scripts/ ./scripts/

# Copy the compiled static frontend assets from Stage 1 into a specific directory
COPY --from=frontend-builder /build/dist ./frontend/dist

# Expose the port Cloud Run expects (defaults to 8080)
EXPOSE 8080

# Run FastAPI using Uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]