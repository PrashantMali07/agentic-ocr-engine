# Troubleshooting Guide

This guide helps diagnose and resolve common issues with the PaddleOCR service.

## General Principles

1. **Check the Logs**: Most issues leave traces in the application logs
2. **Verify Environment**: Ensure required environment variables are set correctly
3. **Check Resources**: Monitor CPU, memory, GPU, and disk usage
4. **Test Connectivity**: Verify the service can reach required endpoints (if any)
5. **Reproduce**: Try to reproduce the issue in a controlled environment

## Log Locations

- **Container stdout/stderr**: Accessible via `docker compose logs` or cloud provider logs
- **Persistent log file**: `blob/output/logs/ingestion.log` (rotating, 5 MB per file, 5 backups)
- **Log format**: `%(asctime)s [%(levelname)s] %(request_id)s %(message)s`

## Common Issues and Solutions

### 1. Service Fails to Start or Crashes Immediately

#### Symptoms
- Container exits with non-zero code
- No health endpoint responding
- Logs show traceback or error messages

#### Possible Causes
- **Missing Environment Variables**: Required variables like `OCR_DPI`, `LOW_DPI`, etc. not set
- **Dependency Issues**: Missing Python packages or native libraries
- **Permission Errors**: Cannot read/write to blob directories
- **GPU Issues**: GPU requested but not available or drivers missing

#### Solutions
1. Check logs for specific error messages
2. Verify all required environment variables are present in `.env` or deployment config
3. Ensure the blob directories (`blob/input`, `blob/output`) exist and are readable/writable
4. If using GPU:
   - Run `nvidia-smi` inside the container to verify GPU visibility
   - Ensure the base image includes CUDA and cuDNN
   - Check that the host has NVIDIA drivers installed and the container runtime supports GPU
5. Try running with CPU-only configuration (`PADDLE_USE_GPU=false`) to isolate GPU issues

### 2. High Latency or Slow Processing

#### Symptoms
- Requests take much longer than expected
- GPU utilization low when it should be high
- Jobs queuing up

#### Possible Causes
- **Falling Back to CPU**: `PADDLE_USE_GPU=false` or GPU not detected
- **Image Size Too Large**: High `OCR_DPI` or large documents causing excessive processing time
- **Concurrency Limit Too Low**: `MAX_CONCURRENT_JOBS` set too low for workload
- **Disk I/O Bottleneck**: Slow blob storage or insufficient IOPS
- **Model Loading Overhead**: Models loaded on first request (should be cached)

#### Solutions
1. Check logs for lines indicating GPU/CPU usage
2. Verify `PADDLE_USE_GPU=true` and that GPU is detected
3. Monitor GPU utilization with `nvidia-smi` (if available)
4. Consider reducing `OCR_DPI` (trade-off between accuracy and speed)
5. Increase `MAX_CONCURRENT_JOBS` if GPU has sufficient memory (beware of OOM)
6. Check blob storage performance; consider upgrading storage tier
7. Warm up the service with a few requests to ensure models are loaded

### 3. Out of GPU Memory (OOM) Errors

#### Symptoms
- Container crashes with OOM errors
- Logs show CUDA out of memory messages
- GPU utilization spikes then drops

#### Possible Causes
- **Too Many Concurrent Jobs**: Each GPU job consumes significant memory
- **Image Dimensions Too Large**: Single image exceeds GPU memory capacity
- **High Precision Data Type**: Using `float32` instead of `uint8` or `float16`
- **Memory Leak**: Unlikely but possible in extended runs

#### Solutions
1. Reduce `MAX_CONCURRENT_JOBS` in `api.py` (default is 2)
2. Lower `OCR_DPI` to reduce input image size
3. Ensure `OCR_MAX_DIM` is set appropriately (default 4000)
4. Consider using `OCR_DTYPE=float16` or `uint8` to reduce memory usage
5. Enable `OCR_GRAYSCALE=true` if color is not needed (halves memory for 3-channel->1-channel)
6. Check for very large documents and consider pre-splitting them

### 4. No Text Extracted or Poor OCR Quality

#### Symptoms
- Empty or garbled text in output
- Low confidence scores
- Missing text that is visible in the source

#### Possible Causes
- **Incorrect File Type Detection**: Service trying to use OCR on digital PDFs or vice versa
- **Poor Quality Input**: Blurry, low-resolution, or badly scanned documents
- **OCR Model Limitations**: Struggling with specific fonts, layouts, or languages
- **Preprocessing Issues**: Enhancement or grayscale settings hurting rather than helping
- **Language Detection Failure**: Text present but language detection returns None incorrectly

#### Solutions
1. Verify the file is being routed to the correct parser (check logs for "Native Parsing" vs "PaddleOCR")
2. Try different preprocessing settings:
   - Toggle `OCR_ENHANCE` (true/false)
   - Toggle `OCR_GRAYSCALE` (true/false)
   - Adjust `OCR_DPI` (sometimes higher DPI helps with small text)
3. Check if the document is password-protected and if the password is provided (not currently supported via API)
4. For digital PDFs that are actually scanned, ensure the service detects them as needing OCR
5. Try preprocessing the document externally (e.g., deskew, denoise) before uploading
6. If specific languages are problematic, consider training custom PaddleOCR models (advanced)

### 5. Validation Failures

#### Symptoms
- `/validate-attachments` reports many files as invalid
- Validation reports show unexpected differences

#### Possible Causes
- **Formatting Differences**: Whitespace, line endings, or encoding differences
- **OCR Errors**: Misrecognized characters leading to text differences
- **Page Splitting Differences**: Different heuristics for what constitutes a "page"
- **Metadata Differences**: Validation compares more than just raw text

#### Solutions
1. Examine the validation report details to see what kind of differences are reported
2. If using `include_diff=true`, review the diff output to understand the nature of changes
3. Consider that some differences are expected (e.g., OCR may normalize ligatures, fix typos)
4. For critical documents, consider a hybrid approach: OCR + manual review
5. Check if the original document has complex layouts (tables, columns) that OCR struggles with

### 6. Job Store Issues

#### Symptoms
- Jobs stuck in "queued" or "processing" state
- Unable to retrieve job results
- Database errors in logs

#### Possible Causes
- **SQLite Lock Contention**: Too many concurrent write attempts
- **Disk Full**: No space left in blob output directory
- **Permissions Lost**: Container cannot write to `blob/output/jobs.db`
- **Corrupted Database**: Rare but possible if container crashes during write

#### Solutions
1. Check available disk space in the blob output partition
2. Verify the container has write permissions to `blob/output`
3. If jobs are stuck, consider restarting the service (SQLite locks are released on process exit)
4. For persistent lock issues, investigate if multiple instances are trying to write to the same DB without proper sharing (should be fine with single instance or shared FS)
5. If corruption is suspected, backup and delete `jobs.db`; it will be recreated on startup (incomplete jobs will be lost)
6. Consider monitoring the size of `jobs.db` and implementing periodic cleanup

### 7. API Returns 500 Errors

#### Symptoms
- Client receives HTTP 500 Internal Server Error
- Logs show exceptions or tracebacks

#### Possible Causes
- **Unhandled Exceptions**: Bugs in the code that weren't caught
- **Invalid Input**: Malformed requests that bypass validation
- **Resource Exhaustion**: Out of memory, disk space, or file descriptors
- **Dependency Failure**: Unable to load OCR models or initialize parsers

#### Solutions
1. Examine the logs for the full traceback to identify the root cause
2. Check if the error is consistent (same request every time) or intermittent
3. If consistent, create a minimal reproducing case and test locally
4. If intermittent, look for resource exhaustion patterns (check memory, disk, GPU usage)
5. Verify all required dependencies are present and correctly versioned
6. Consider adding more specific error handling if patterns emerge

### 8. 400 Bad Request Errors

#### Symptoms
- Client receives HTTP 400 Bad Request
- Response includes validation error details

#### Possible Causes
- **Missing Required Fields**: Required form fields not provided
- **File Type Not Supported**: Uploaded file extension not in allowed list
- **Empty Files**: Uploaded files with zero bytes
- **Too Many Files**: Exceeding implicit or explicit limits

#### Solutions
1. Check the response body for specific validation error messages
2. Ensure the request matches the expected API contract (see OpenAPI spec)
3. Verify file extensions are supported: .pdf, .jpg, .jpeg, .png, .bmp, .tif, .tiff, .webp, .docx, .html, .htm, .xls, .xlsx
4. Check file sizes and contents before uploading
5. Reduce the number of files in a single request if hitting limits

### 9. Service Not Responding / Timeout

#### Symptoms
- Requests hang and eventually timeout
- No logs appear for the request
- Health endpoint also unresponsive

#### Possible Causes
- **Event Loop Blocked**: Synchronous blocking code in async context (e.g., long-running OCR call not offloaded to thread pool)
- **Deadlock**: Resource contention causing threads to wait indefinitely
- **Network Issues**: Service not binding to correct interface or firewall blocking
- **Container Overloaded**: Too many requests exhausting resources

#### Solutions
1. Check logs to see if the request is being received at all
2. Verify the service is listening on `0.0.0.0:8000` (not `127.0.0.1`)
3. Look for long-running blocking operations in the code that should be async or offloaded
4. If using Docker, check container resource limits and usage
5. Restart the service to clear any deadlocked state
6. Consider adding timeouts to external operations if possible

### 10. Issues Specific to Azure Container Apps

#### Symptoms
- Deployment succeeds but service doesn't respond
- Health checks fail from platform perspective
- Logs show platform-specific errors

#### Possible Causes
- **Incorrect Image Reference**: Typo in image name or missing ACR credentials
- **Environment Variables Missing**: Required variables not set in Container App configuration
- **Ingress Not Configured**: Service listening but not exposed via ingress
- **Storage Mount Issues**: Blob storage not mounted correctly or permissions missing
- **Scaling to Zero**: If minimum replicas is 0 and no traffic, may take time to scale up

#### Solutions
1. Check the Container App's "Deployment Center" for image pull logs
2. Verify environment variables in the Container App configuration match `.env`
3. Test ingress by trying to reach the service URL from outside the VNet
4. Check storage connections: "Settings" -> "Storage" in Azure Container Apps portal
5. Set minimum replicas to at least 1 if you need immediate availability
6. Check diagnostic logs: "Monitoring" -> "Logs" in Azure Container Apps portal
7. Verify that the container port (8000) matches what the service is configured to listen on

## Diagnostic Commands

### When Running Locally with Docker Compose
```bash
# View logs in real-time
docker compose logs -f

# Check container resource usage
docker compose ps
docker compose top

# Exec into a running container
docker compose exec <service-name> /bin/bash

# Check disk usage
docker system df

# Inspect image layers
docker image inspect <image-name>
```

### When Deployed to Azure Container Apps
```bash
# Azure CLI commands
az containerapp show --name indolog-ocr --resource-group <rg>
az containerapp logs show --name indolog-ocr --resource-group <rg> --tail 100
az containerapp ingress show --name indolog-ocr --resource-group <rg>
```

## When to Escalate

Consider escalating to the platform/backend team if:
- The issue persists after checking logs and trying common solutions
- It involves infrastructure beyond the container (networking, storage, GPU allocation)
- You suspect a bug in the core OCR engine or dependencies that requires code changes
- The issue affects multiple instances or is systemic
- You need to modify deployment configurations or access restricted resources

## Reporting Issues

When reporting an issue, please include:
1. **Environment**: Local Docker, Azure Container Apps, etc.
2. **Steps to Reproduce**: Exact commands or API calls
3. **Logs**: Relevant excerpts from `ingestion.log` or container logs
4. **Environment Variables**: Non-secret variables that affect behavior
5. **Sample Data**: If possible, a minimally reproducing document (redact sensitive info)
6. **Expected vs Actual Behavior**: Clear description of what went wrong

## Quick Reference: Environment Variable Effects

| Variable | Affects | Common Values | Troubleshooting Tip |
|----------|---------|---------------|---------------------|
| `PADDLE_USE_GPU` | GPU/CPU selection | `true`/`false` | Set to `false` to isolate GPU issues |
| `OCR_DPI` | PDF rendering density | `100-400` | Lower for speed, higher for accuracy |
| `OCR_ENHANCE` | Image preprocessing | `true`/`false` | Try toggling if OCR quality poor |
| `OCR_MAX_DIM` | Max image dimension | `3000-4000` | Lower if seeing OOM errors |
| `OCR_DTYPE` | Data type for OCR | `uint8`, `float16`, `float32` | Use `uint8` or `float16` to save GPU memory |
| `OCR_GRAYSCALE` | Color to grayscale | `true`/`false` | Set to `true` to halve memory for color images |
| `MAX_CONCURRENT_JOBS` | (in api.py) Concurrent GPU jobs | `1-4` | Increase if GPU underutilized, decrease if OOM |

## Maintenance Tips

- Regularly monitor log files for recurring warnings or errors
- Keep base images updated with security patches
- Test backup and restore procedures periodically
- Document any configuration changes made for troubleshooting
- Consider setting up alerts for:
  - Container crash loops
  - High memory/CPU usage (>80% for extended periods)
  - Failed health checks
  - Long job queue times