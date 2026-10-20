# PaddleOCR on Azure — options, minimums, and cost

This doc explains how IndoLog runs OCR, what was built, and what each Azure option costs in **Central India (INR, pay-as-you-go list prices)**. Prices change; treat numbers as planning estimates.

---

## 1. How OCR fits in IndoLog

PaddleOCR is a **separate container**, not inside n8n or Desk.

```
Email attachments → Azure Blob (indolog-attachments)
                         ↓
              n8n calls OCR over HTTP
                         ↓
              POST {OCR_URL}/extract
                         ↓
              OCR container (Paddle) → text back to n8n
```

| Piece | Name / value |
|--------|----------------|
| Local compose service | `ocr` |
| Local container name | `indolog-ocr` |
| n8n env | `OCR_URL=http://ocr:8000` (local) or HTTPS URL (Azure) |
| ACR image (GPU, built) | `indologitomata.azurecr.io/indolog-ocr-gpu:latest` |
| ACR build run | `cu3` — **Succeeded** |
| ACA OCR app (deployed) | `indolog-ocr` — GPU T4 profile `t4gpu`, **2 vCPU / 8 GiB**, min=0 |
| Deploy script | `n8n community/scripts/azure-deploy-ocr.ps1` |

n8n and Desk stay on CPU Container Apps. Only the OCR service needs GPU if you choose a GPU path.

---

## 2. What you already pay (without OCR)

Resources in resource group **Indolog** (Central India unless noted):

| Service | Resource | Typical config | Rough monthly feel |
|---------|----------|----------------|--------------------|
| Container App | `indologcontainerapp` (n8n) | 1 vCPU, 2 GiB, min=1 | High if always on |
| Container App | `indologdesk` | 0.5 vCPU, 1 GiB, min=1 | Medium if always on |
| Postgres | `indologpg` | B1ms + 32 GB | ~₹1.7k compute + storage |
| ACR | `indologitomata` Basic | registry | ~₹480/month + image storage |
| Blob | `indologmlstorage` Hot LRS | attachments + ML | low at small volume |
| Log Analytics / App Insights | workspaces | 30-day | usage-based |
| Key Vault | `indolog-dev-env-vault` (West India) | Standard | usually tiny |
| Azure ML workspace | `indologml` | workspace only | ~₹0 when idle |

**ACA CPU rates (Central India):**

- Active vCPU: **₹0.0023 / second**
- Active memory: **₹0.0003 / GiB-second**
- Idle (replica up, little traffic): cheaper vCPU rate **₹0.0003 / second**
- Free grant / month (shared): **180,000 vCPU-s** + **360,000 GiB-s** + **2M requests**

**Example — n8n + desk for 30 hours (both up):**

| Mode | Together |
|------|----------|
| Busy (active) | ~**₹470** |
| Mostly idle | ~**₹146** |
| First ~33 h/month of this size | often covered by free grant |

**Example — n8n + desk per day (24 h):**

| Mode | Together |
|------|----------|
| Active | ~**₹375** |
| Mostly idle | ~**₹117** |

---

## 3. GPU OCR options (the expensive part)

### A) Azure Container Apps — serverless GPU

| Item | Value |
|------|--------|
| Only T4 profile in Central India | `Consumption-GPU-NC8as-T4` |
| GPU | 1× NVIDIA T4 (16 GB) — you pay full GPU while replica is up |
| You can set CPU/RAM | 0.25–8 vCPU, 0.5–56 GiB |
| GPU meter | **₹0.0097 / GPU-second** (~**₹35 / hour** for GPU alone) |

**Cost while OCR replica is running:**

| Sizing | ₹ / hour | ₹ / day (24 h on) | Notes |
|--------|----------|-------------------|--------|
| 0.25 vCPU + 0.5 GiB + T4 | ~**₹38** | ~**₹900** | Azure minimum — **too small for Paddle** |
| **2 vCPU + 8 GiB + T4** | ~**₹60** | ~**₹1,450** | Practical minimum |
| **4 vCPU + 16 GiB + T4** | ~**₹85** | ~**₹2,050** | Safer for PP-Structure |
| 8 vCPU + 56 GiB + T4 | ~**₹162** | ~**₹3,880** | Full profile (expensive) |

- **Scale to zero (`minReplicas=0`)** → ~**₹0** when no OCR.
- Serverless GPU has **no cheap idle rate**: if the replica is up, you pay.

**n8n + desk + always-on ACA GPU (full profile):** ~**₹4,250 / day**.

---

### B) Azure GPU VM (start / stop)

Smallest T4 VM in Central India:

| SKU | Specs | PAYG ₹/h | Spot ₹/h | Low priority ₹/h |
|-----|--------|----------|----------|------------------|
| **`Standard_NC4as_T4_v3`** (minimum) | 4 vCPU, 28 GiB, 1× T4 16 GB | ~**₹55** | ~**₹16** | ~**₹11** |
| `Standard_NC8as_T4_v3` | 8 vCPU, 56 GiB, 1× T4 | ~**₹79** | ~**₹22** | ~**₹16** |
| `Standard_NC16as_T4_v3` | 16 vCPU, 110 GiB, 1× T4 | ~**₹127** | ~**₹36** | ~**₹25** |

Deallocate when idle → compute ~**₹0** (disk may still cost a little).

---

### C) CPU PaddleOCR on Container Apps (no GPU)

Use CPU image (`Dockerfile`, not `Dockerfile.gpu`). Same HTTP API.

| Sizing | ₹ / hour | ₹ / day always on |
|--------|----------|-------------------|
| 1 vCPU + 2 GiB | ~**₹10** | ~**₹251** |
| 2 vCPU + 4 GiB | ~**₹21** | ~**₹500** |

With **`minReplicas=0`**: ~**₹0** idle; pay only while OCR runs.  
Tradeoff: slower than GPU; usually fine for intermittent mail attachments.

---

### D) Azure Document Intelligence (managed API)

No GPU container. Pay **per page**.

| Model | ~₹ / 1,000 pages | ~₹ / page |
|-------|------------------|-----------|
| Read (plain OCR) | ~**₹143** | ~**₹0.14** |
| Layout / prebuilt | ~**₹955** | ~**₹0.96** |
| Free F0 | 500 pages/month | limits apply |

Example: **50 pages/day** with Read ≈ **₹7/day**; Layout ≈ **₹48/day**.

---

### E) Local / office NVIDIA + Docker

Run `indolog-ocr` on a machine with GPU. Cloud cost for OCR ≈ **₹0**.  
n8n on Azure must reach it (VPN / reverse proxy / tunnel) or run OCR only for local/dev.

---

## 4. Same workload compared

| Option | 1 hour OCR/day | ~50 pages/day | Always on 24h |
|--------|----------------|---------------|---------------|
| ACA GPU (practical 2–4 CPU) | ~₹60–85 | same if job ~1h | ~₹1.4k–2.1k |
| ACA GPU (full 8/56) | ~₹162 | same | ~₹3,880 |
| VM NC4as T4 PAYG (1h on) | ~₹55 | ~₹55 | ~₹1,300 |
| VM NC4as Spot (1h) | ~₹16 | ~₹16 | eviction risk |
| ACA CPU Paddle | ~₹10 | ~₹10 if ~1h | ~₹251 |
| Doc Intelligence Read | n/a | ~₹7 | n/a |
| Local GPU | ~₹0 | ~₹0 | ~₹0 |

**Rule of thumb**

- Low mail volume → **CPU ACA** or **Document Intelligence**
- Need Paddle GPU sometimes → **VM NC4 start → OCR → stop** (cheaper than ACA GPU per hour)
- Avoid **ACA GPU always on**

---

## 5. Recommended path for IndoLog

1. **Production Azure (cheap):** CPU OCR Container App, `minReplicas=0`, set n8n `OCR_URL` to that HTTPS URL.  
2. **Need GPU speed:** `NC4as_T4_v3` VM, start only when needed; use image `indologitomata.azurecr.io/indolog-ocr-gpu:latest`.  
3. **Dev:** local Docker GPU compose service `ocr` / `indolog-ocr`.  
4. **Do not** leave serverless ACA T4 at min=1 unless you accept ~₹1.5k–4k/day.

---

## 6. Deploy checklist (when you choose)

### Shared

- [ ] Image in ACR: `indologitomata.azurecr.io/indolog-ocr-gpu:latest` (done) or CPU image if choosing CPU
- [ ] Set n8n app setting `OCR_URL` to OCR base URL (no `/extract` suffix)
- [ ] Keep Blob enabled for attachments

### If ACA GPU

- [ ] Request **Managed Environment Consumption T4 Gpus** quota if blocked
- [ ] Add profile `t4gpu` type `Consumption-GPU-NC8as-T4` on `managedEnvironment-Indolog-98d6`
- [ ] Create app e.g. `indolog-ocr` on that profile, **min=0**, CPU/RAM **2–4 / 8–16 GiB** (not full 8/56 unless needed)

### If GPU VM

- [ ] Create `Standard_NC4as_T4_v3` in Central India
- [ ] Install NVIDIA + Docker, pull ACR image, run on port 8000
- [ ] Start/stop script around OCR batches

### If CPU ACA

- [ ] Build/push CPU image from `paddleocr/Dockerfile`
- [ ] Create Consumption app (no GPU profile), min=0, e.g. 1–2 vCPU / 2–4 GiB

---

## 7. Quick glossary

| Term | Meaning |
|------|---------|
| **ACA** | Azure Container Apps (where n8n/desk run today) |
| **Serverless GPU** | ACA T4 profile; pay per second while replica runs |
| **Scale to zero** | `minReplicas=0` — no compute charge when idle |
| **OCR_URL** | Base URL n8n uses; calls `POST /extract` |
| **Blob** | Attachment source of truth (`indolog-attachments`) |

---

## 8. Official links

- [ACA serverless GPU](https://learn.microsoft.com/en-us/azure/container-apps/gpu-serverless-overview)
- [ACA pricing (India)](https://azure.microsoft.com/en-in/pricing/details/container-apps/)
- [NCasT4_v3 VM sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ncast4v3-series)
- [Document Intelligence pricing](https://azure.microsoft.com/en-in/pricing/details/document-intelligence/)

---

*Generated for IndoLog planning. Verify live rates in Azure Pricing Calculator / Cost Management before committing spend.*
