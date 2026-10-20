# Build + push GPU PaddleOCR image via ACR Tasks (no local Docker GPU needed).
# Usage (from repo root or this folder):
#   powershell -File services/paddleocr/scripts/azure-build-ocr-gpu.ps1
#
# Env overrides: AZURE_RG, ACR_NAME, OCR_IMAGE_TAG

$ErrorActionPreference = "Stop"
$az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
$paddle = Resolve-Path (Join-Path $PSScriptRoot "..")

$rg = if ($env:AZURE_RG) { $env:AZURE_RG } else { "Indolog" }
$acr = if ($env:ACR_NAME) { $env:ACR_NAME } else { "indologitomata" }
if (-not $env:OCR_IMAGE_TAG -or $env:OCR_IMAGE_TAG -eq "latest") {
  throw "Set OCR_IMAGE_TAG to a reviewed release identifier; building or overwriting latest is disabled."
}
$tag = $env:OCR_IMAGE_TAG
$image = "indolog-ocr-gpu:$tag"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try {
  chcp 65001 | Out-Null
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
  $OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch {}

Write-Host "Building $image in ACR=$acr RG=$rg from $paddle (Dockerfile.gpu)"

Push-Location $paddle
try {
  & $az acr build `
    --registry $acr `
    --resource-group $rg `
    --image $image `
    --file "./Dockerfile.gpu" `
    --timeout 10800 `
    --no-logs `
    .
  if ($LASTEXITCODE -ne 0) { throw "acr build queue failed: $LASTEXITCODE" }

  $runs = & $az acr task list-runs --registry $acr --resource-group $rg --top 1 -o json | ConvertFrom-Json
  $runId = $runs[0].runId
  Write-Host "Queued run $runId - polling until Succeeded/Failed..."

  while ($true) {
    $run = & $az acr task show-run --registry $acr --run-id $runId -o json | ConvertFrom-Json
    $status = $run.status
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $status)
    if ($status -in @("Succeeded", "Failed", "Canceled", "Timeout", "Error")) { break }
    Start-Sleep -Seconds 30
  }

  if ($run.status -ne "Succeeded") {
    & $az acr task logs --registry $acr --run-id $runId 2>&1 | Select-Object -Last 40
    throw "acr build failed: $($run.status)"
  }
}
finally {
  Pop-Location
}

Write-Host "Done. Image: $acr.azurecr.io/$image"
Write-Host "Next: powershell -File services/paddleocr/scripts/azure-deploy-ocr.ps1"
