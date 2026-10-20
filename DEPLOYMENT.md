# Azure Container Apps Deployment Guide

## Overview

This document describes how to deploy the LogisticAI Ingestion Pipeline to Azure Container Apps.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Client App    │────▶│  Azure Container │────▶│  Azure Container│
│  (API Consumer) │     │      Apps        │     │    Registry     │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                              │
                              ▼
                       ┌──────────────────┐
                       │  Application     │
                       │   Insights       │
                       └──────────────────┘
```

## Prerequisites

- Azure CLI installed and logged in (`az login`)
- Azure subscription with Contributor access
- Docker (for local testing only - ACR Tasks builds in cloud)

## Quick Start (Automated)

```bash
# Clone and navigate
cd ingestion-pipeline

# Run deployment script
./infra/deploy.sh my-resource-group eastus myacrname
```

This script will:
1. Create resource group
2. Create Azure Container Registry (ACR)
3. Build and push Docker image via ACR Tasks
4. Create Container App Environment
5. Deploy Container App via Bicep
6. Output the application URL

## Manual Deployment Steps

### 1. Create Resource Group

```bash
az group create --name logistic-ai-rg --location eastus
```

### 2. Create Azure Container Registry

```bash
az acr create \
    --resource-group logistic-ai-rg \
    --name logisticaiacr \
    --sku Basic \
    --admin-enabled true
```

### 3. Build and Push Image

**Option A: Using ACR Tasks (recommended - no local Docker needed)**

```bash
az acr build \
    --registry logisticaiacr \
    --image logistic-ai-ingestion:latest \
    --file Dockerfile \
    .
```

**Option B: Local Docker Build**

```bash
# Login to ACR
az acr login --name logisticaiacr

# Build and push
docker build -t logisticaiacr.azurecr.io/logistic-ai-ingestion:latest .
docker push logisticaiacr.azurecr.io/logistic-ai-ingestion:latest
```

### 4. Create Container App Environment

```bash
az containerapp env create \
    --name logistic-ai-env \
    --resource-group logistic-ai-rg \
    --location eastus
```

### 5. Deploy Container App

**Using Bicep (recommended):**

```bash
ACR_LOGIN_SERVER=$(az acr show --name logisticaiacr --query loginServer -o tsv)
ACR_USERNAME=$(az acr credential show --name logisticaiacr --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name logisticaiacr --query passwords[0].value -o tsv)

az deployment group create \
    --resource-group logistic-ai-rg \
    --template-file infra/main.bicep \
    --parameters \
        containerAppName=logistic-ai-ingestion \
        containerAppEnvName=logistic-ai-env \
        containerRegistryServer="$ACR_LOGIN_SERVER" \
        containerRegistryUsername="$ACR_USERNAME" \
        containerRegistryPassword="$ACR_PASSWORD" \
        imageName=logistic-ai-ingestion \
        imageTag=latest
```

**Using Azure CLI directly:**

```bash
az containerapp create \
    --name logistic-ai-ingestion \
    --resource-group logistic-ai-rg \
    --environment logistic-ai-env \
    --image logisticaiacr.azurecr.io/logistic-ai-ingestion:latest \
    --registry-server logisticaiacr.azurecr.io \
    --registry-username "$ACR_USERNAME" \
    --registry-password "$ACR_PASSWORD" \
    --target-port 8000 \
    --ingress internal \
    --min-replicas 1 \
    --max-replicas 10 \
    --cpu 2 \
    --memory 4Gi \
    --env-vars \
        PYTHONUNBUFFERED=1 \
        PADDLE_USE_GPU=false \
        USE_TENSORRT=false \
        USE_ONNX=false
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8000 | HTTP port |
| `PYTHONUNBUFFERED` | 1 | Unbuffered Python output |
| `PADDLE_USE_GPU` | false | Enable GPU (requires GPU container) |
| `USE_TENSORRT` | false | Use TensorRT optimization |
| `USE_ONNX` | false | Use ONNX runtime |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | - | App Insights connection string |

### Scaling Configuration

The Container App scales based on CPU utilization:
- Min replicas: 1
- Max replicas: 10
- Scale trigger: CPU > 70%

Adjust in `infra/main.bicep`:
```bicep
param minReplicas int = 1
param maxReplicas int = 10
param targetCpuUtilization int = 70
```

### Resource Limits

```bicep
param cpuCores string = '2'
param memory string = '4Gi'
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness check |
| `/ready` | GET | SQLite, worker heartbeat, and disk readiness check |
| `/operations/metrics` | GET | Admin-only local queue and retention metrics |
| `/extract/v1` | POST | Submit a durable extraction job |
| `/extract/v1/{job_id}` | GET/DELETE | Poll or cancel a job |
| `/extract/v1/{job_id}/review` | POST | Tenant-scoped reviewer correction |
| `/extract/v1/{job_id}/reviews` | GET | Tenant-scoped review history |
| `/extract` | POST | Disabled by default; local compatibility opt-in only |
| `/extract-dir` | POST | Disabled server-directory route |
| `/validate-attachments` | POST | Disabled by default; local compatibility opt-in only |
| `/batch-extract` | POST | Disabled server-directory route |

### Example Requests

**Extract from uploaded files:**
```bash
curl -X POST https://<app-url>/extract \
  -F "files=@document.pdf" \
  -F "files=@image.png" \
  -F "request_id=req-123" \
  -F "save_visualizations=false"
```

**Extract from server directory:**
```bash
curl -X POST https://<app-url>/extract-dir \
  -F "directory_path=/data/inbox/batch_001" \
  -F "request_id=req-456" \
  -F "save_visualizations=false"
```

**Validate attachments:**
```bash
curl -X POST https://<app-url>/validate-attachments \
  -F "files=@document.pdf" \
  -F "request_id=req-789" \
  -F "include_diff=true"
```

**Batch extract:**
```bash
curl -X POST https://<app-url>/batch-extract \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "batch-001",
    "directories": [
      {"request_id": "req-001", "directory_path": "/data/batch_001"},
      {"request_id": "req-002", "directory_path": "/data/batch_002"}
    ],
    "save_visualizations": false
  }'
```

## Monitoring

### Application Insights

Enable by setting `APPLICATIONINSIGHTS_CONNECTION_STRING` in Container App environment variables.

### Health Checks

- Liveness: `GET /health` (every 30s, starts after 60s)
- Readiness: `GET /ready` (every 10s, starts after the worker begins polling)

### Logs

```bash
# View logs
az containerapp logs show \
    --name logistic-ai-ingestion \
    --resource-group logistic-ai-rg \
    --follow
```

## CI/CD Pipeline

The GitHub Actions workflow (`.github/workflows/ci-cd.yml`) runs on every push to main:

1. **Lint & Test** - Ruff, MyPy, Pytest
2. **Build** - Docker image via Buildx, push to ACR
3. **Deploy** - Bicep deployment to Azure Container Apps

### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `AZURE_CREDENTIALS` | Service principal JSON for Azure login |
| `ACR_LOGIN_SERVER` | ACR login server (e.g., `myacr.azurecr.io`) |
| `ACR_USERNAME` | ACR admin username |
| `ACR_PASSWORD` | ACR admin password |
| `APP_INSIGHTS_CONNECTION_STRING` | Optional App Insights connection string |

## Local Development

```bash
# Start with docker-compose
docker-compose up --build

# API available at http://localhost:8000
# Docs at http://localhost:8000/docs
```

## Troubleshooting

### Container App fails to start

```bash
# Check logs
az containerapp logs show -n logistic-ai-ingestion -g logistic-ai-rg

# Check events
az containerapp show -n logistic-ai-ingestion -g logistic-ai-rg --query properties.provisioningState
```

### Image pull errors

```bash
# Verify ACR credentials
az acr credential show -n logisticaiacr

# Check image exists
az acr repository show-tags -n logisticaiacr --repository logistic-ai-ingestion
```

### OCR Model Download Issues

First request may take 60-120s as PaddleOCR downloads models. Subsequent requests are fast.

```bash
# Pre-warm by calling health endpoint
curl https://<app-url>/health
```

### Increase timeout for large files

```bash
az containerapp update \
    -n logistic-ai-ingestion -g logistic-ai-rg \
    --set-env-vars REQUEST_TIMEOUT=600
```

## Security

- Container runs as non-root user (`appuser`)
- No privileged containers
- Ingress only on port 8000
- HTTPS enforced
- System-assigned managed identity

## Cost Optimization

- Set `minReplicas: 0` for dev/test (scales to zero)
- Use `Consumption` plan for sporadic workloads
- Monitor with `az consumption usage list`

## Cleanup

```bash
# Delete resource group (deletes everything)
az group delete --name logistic-ai-rg --yes --no-wait
```
