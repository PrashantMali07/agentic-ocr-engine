# GPU load, image smoke, vulnerability scan, and SBOM plan

Status: **representative FastAPI load harness implemented, not executed** (19 October 2026). No image build, GPU soak, image scan, or deployment was performed. The supported target is FastAPI `POST /extract/v1` + `GET /extract/v1/{job_id}`, the `legacy` PP-StructureV3/Tesseract path, one SQLite-backed host, and one OCR child at a time. The earlier RTX 2060 FastAPI run completed three scans; it is a functional baseline, not a release-capacity result.

## Gate A — Define the release workload before measuring

The business owner and performance owner must sign the [quality policy template](docs/runbooks/phase06-quality-policy.example.json) before the release run. Record maximum CER/WER, minimum field-value presence and correct label/table association, maximum failure/retry rate, maximum p95/p99 end-to-end latency, minimum throughput, maximum RAM/VRAM/artifact bytes, minimum run duration, and at least 100 completed GPU jobs. Define whether `partial` and `waiting_for_review` count as successful for each document type; the present gate treats both as successful terminal states, so business policy must decide if that is acceptable. Do not choose thresholds after seeing results.

Inventory the real workload by format (scanned/digital PDFs, images, DOCX/XLSX), page count, resolution, file size, language, tables, and difficult scans. Keep private source documents in `test_asset` or another restricted location; save aggregate evidence in ignored, owner-only `phase06_evidence/`. Split the corpus into: (1) independently reviewed accuracy scans; (2) load documents of realistic variety; (3) hard-but-valid limits near the allowed maxima. Include negative cases separately (oversized, corrupt, encrypted, unsupported), and never use customer data without authorization.

The scanned-image reference set may be **drafted by a VLM and then corrected by an independent reviewer**. The [VLM test-suite plan](VLM_TEST_SUITE.MD) defines image rendering, draft provenance, full-transcript review, table association checks, and comparison with this OCR pipeline. Raw VLM output is not release ground truth.

**Why the old harness is not the whole soak:** `scripts/validate_phase04_fastapi_gpu.py --rounds 34` submits 102 jobs through FastAPI, but repeats only three scanned PDFs in batches of three. `scripts/measure_representative_gpu.py` now accepts a bounded, versioned workload manifest that varies files, document classes, test tenants, and scheduled arrival rates through the same authenticated route. Its private per-job JSON records fixture hashes, sizes, tenant, admission response, job ID, status, and timing; the aggregate JSON contains no document text. The [example manifest](docs/runbooks/representative-gpu-workload.example.json) has only eight jobs and is a contract example, not a release workload.

## Gate B — Prepare a reproducible test host

1. Select the intended GPU model, VRAM, CPU/RAM, local scratch volume, driver, container toolkit, and OS. Record exact versions, GPU UUID, image/commit digest, model manifest digest, admission configuration, number of API/worker processes, and clock time. Run `nvidia-smi` on the host; verify Docker can expose the GPU to a known CUDA image. NVIDIA documents a container-toolkit sample workload using `docker run --rm --gpus all ... nvidia-smi` ([NVIDIA guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html)). Do not interpret a host-only `nvidia-smi` result as proof the container can use the GPU.
2. Reserve enough free disk for the multi-gigabyte build context, model downloads, image layers, temporary OCR files, and scanner databases. Keep the OCR data volume on the same host for API and worker. Confirm the test credentials map to a dedicated test tenant and never appear in the evidence files.
3. Ensure port 8000 is free before the local harness. Preserve a baseline of idle RAM/VRAM/disk, `/ready`, and `/operations/metrics`. Run the existing 57-test CPU suite and offline lock check first; an online advisory check also belongs in release CI.

**Stop condition:** no GPU inside a container, insufficient disk, missing model provenance, mismatched API/worker storage, or a failing readiness/lock check. Record the cause and repair it before continuing.

## Gate C — Build and smoke-test the GPU image (later, when authorized)

1. Review `Dockerfile.gpu`, `uv.lock`, `requirements-cpu.lock`, and `model-artifacts.json`. The base-image digests and Python/model files are pinned, but `apt-get` still resolves packages from changing distro repositories; record the resolved OS package list and tighten that supply-chain gap before calling the build fully reproducible. Confirm model hashes against an independently approved publisher source; a hash copied from a workstation cache alone proves consistency, not provenance.
2. Build a uniquely tagged local candidate from the repository root, for example `docker build --pull -f Dockerfile.gpu -t ocr-gpu:release-candidate .`. Record the exact image ID with `docker image inspect`. Do not use `latest`, and do not push or deploy until all gates pass.
3. First smoke-test model integrity and GPU initialization with the image **without runtime network access**. Example:

   ```bash
   docker run --rm --gpus all --network none --entrypoint /app/.venv/bin/python ocr-gpu:release-candidate -m ingestion_pipeline.model_artifacts verify --root /home/appuser/.paddlex/official_models --manifest /app/model-artifacts.json
   docker run --rm --gpus all --network none --entrypoint /app/.venv/bin/python ocr-gpu:release-candidate -c 'import paddle; assert paddle.get_device().startswith("gpu:")'
   ```

   Docker's `none` network creates only loopback inside the container ([Docker documentation](https://docs.docker.com/engine/network/drivers/none/)); this tests that model verification and GPU initialization do not require a download. Next start an API and worker container on the same host with the same persistent test volume, GPU access, owner-only test credentials, and no outbound network. Query `/health` and `/ready` from inside the API container, submit at least one scanned PDF via localhost, and confirm a nonempty terminal result and one OCR child. Do not claim that the two one-shot commands alone prove full OCR works.
4. Verify the runtime user is non-root, packaged model files are read-only, the output and scratch paths are writable, missing/changed model files fail startup, and failed OCR leaves no leaked temp data. Test a read-only root filesystem with explicit writable output/tmp mounts only after ordinary smoke passes; do not assume Paddle/Tesseract will work under that restriction without measurement.

**Evidence:** build log, image ID, resolved OS/Python package list, model verification, GPU device output, API/worker smoke log, and network-isolation observation. **Pass condition:** the exact candidate image starts and completes OCR on GPU without any runtime model download or unexpected outbound access.

## Gate D — Scan the exact candidate and emit an SBOM

Use fixed, reviewed versions of the scanners and keep their vulnerability databases current. Scan the **same local image ID/digest** that passed Gate C, covering OS and Python packages. One possible toolchain is:

```bash
trivy image --image-src docker --scanners vuln --severity HIGH,CRITICAL --format json --output phase06_evidence/gpu-vulnerabilities.json ocr-gpu:release-candidate
trivy image --image-src docker --scanners vuln --severity HIGH,CRITICAL --exit-code 1 ocr-gpu:release-candidate
syft docker:ocr-gpu:release-candidate -o spdx-json=phase06_evidence/gpu-sbom.spdx.json
```

Trivy searches the local Docker engine for an image and can enforce a nonzero exit for selected severities ([image target](https://trivy.dev/docs/dev/guide/target/container_image/), [CLI reference](https://trivy.dev/docs/dev/references/configuration/cli/trivy_image/)); Syft can emit SPDX JSON from a Docker image ([Syft documentation](https://github.com/anchore/syft/blob/main/README.md)). Review findings for reachability, available fixes, and compensating controls. A release-blocking finding needs a fix or a documented, time-bounded security exception by the owner; a scanner's empty output does not prove the image is safe. Attach tool/database versions and image ID to both reports. Repeat the scan after any rebuild.

**Stop condition:** unresolved release-blocking advisory, incomplete SBOM, different image ID between smoke and scan, or missing approved model provenance.

## Gate E — Run the real FastAPI GPU workload

1. Use a disposable database/output directory and dedicated test credentials. Run a one-job warm-up, exclude its time from steady-state statistics, then record both **cold-start** time and steady-state runs. Capture host `nvidia-smi` utilization/total VRAM, container RSS/CPU, disk usage/inodes, cgroup OOM counters, API/worker restarts, and application `/operations/metrics`. The app's `peak_gpu_bytes` is sampled per OCR process, not whole-device VRAM, and `peak_artifact_bytes` excludes cached models.
2. As a bounded regression, run `PYTHONPATH=src .venv/bin/python scripts/validate_phase04_fastapi_gpu.py --rounds 34 --output phase06_evidence/gpu_scan_regression.json` on the GPU host. The script starts `uv run --no-sync ingest-api` and two worker processes, uses temporary SQLite, authenticates through HTTP, and submits 102 jobs. It uses a 15-minute deadline **per three-job batch** and refuses to occupy port 8000 if another service is there. Its output is aggregate only and the temporary job data/logs are removed at exit. Keep this output separate from the representative `gpu_load.json` release evidence.
3. For representative capacity, author a private workload manifest using the example schema. Each series specifies a `test_asset` fixture, document class, test tenant, count, start time, and interval; set `expect` to `rejected` only in a separate burst profile. Run `PYTHONPATH=src .venv/bin/python scripts/measure_representative_gpu.py --manifest phase06_evidence/workload.json --output phase06_evidence/gpu_load.json --jobs-output phase06_evidence/gpu_jobs.json`. Outputs must be in a private (0700) directory and existing evidence is never overwritten. The script starts local FastAPI plus workers, uses temporary SQLite and one credential per test tenant, and removes job data/logs on exit. The healthy release profile needs at least 100 admitted jobs, three distinct fixture sizes, two document classes, two tenants, varied arrival intervals, and the owner-approved duration. Use a separate burst profile for expected HTTP 429 responses. Measure isolated scan latency, normal arrivals, a burst near configured queue limits, two tenants competing, and large valid files; do not raise admission limits until measured capacity supports it.

   | Stage | Offered work | What it establishes |
   | --- | --- | --- |
   | Cold + warm | One first request, then several isolated requests per document class | Startup/model cost and single-job service time, reported separately. |
   | Steady | Sustained arrivals at roughly 50%, 75%, then 90% of the measured single-worker completion rate | Where queue wait starts growing; stop a stage if backlog rises without recovery. |
   | Burst | Up to eight active jobs globally and four per tenant, then one more submission | Queue serialization, fair tenant admission, and expected HTTP 429 at the configured ceiling. |
   | Soak | At least 100 jobs over the owner-approved minimum duration, with realistic scan/page-size mix | p95/p99, throughput, retries, memory/disk trend, and cleanup over time. Prefer substantially more than 100 jobs when feasible; at 100 observations the nearest-rank p99 is only the 99th ordered sample. |
   | Faults | Separate controlled worker/child restart with queued work | Durable recovery and bounded retry, without contaminating healthy latency statistics. |

   If the actual traffic mix includes Office files, digital PDFs, and images, report each class separately as well as the aggregate. A fast digital-PDF majority must not hide unacceptable scanned-PDF latency.
4. Test resilience in staging: restart an idle worker with queued jobs, terminate an OCR child once to observe the bounded retry, and let the worker recover. Verify no stranded jobs, duplicate terminal results, cross-tenant reads, or hidden `waiting_for_review` state. Keep fault injection separate from the healthy performance sample so it does not distort p95/p99.
5. Compare all results with the pre-approved policy. `scripts/check_phase06_quality.py` now requires the hashed workload manifest and per-job evidence alongside independently reviewed accuracy and itemized associations; see [Phase 6 release evidence](docs/runbooks/phase06-release-evidence.md). Host-level whole-device VRAM, disk, and container traces still require separate collection and reviewer interpretation.

**Evidence:** exact workload manifest and source hashes, timestamps, raw per-job metrics (kept private), aggregate p50/p95/p99, admissions/retries/failures, host and process resource traces, readiness/alert history, and reviewer interpretation. **Pass condition:** all policy thresholds pass with no unexplained resource growth, page loss, stuck jobs, or readiness failures. If the run fails, change one cause at a time, rebuild/retest when code or image changes, and retain the failed evidence.

## Release decision

The GPU image, load, accuracy, and [production operations plan](PRODUCTION_OPERATION.MD) are separate gates. A successful 102-job repeated-fixture run does not imply broad OCR accuracy; a clean image scan does not imply safe ingress or backups. Release only the exact image digest and configuration that passed every applicable gate, with an explicit rollback and queue-drain procedure. Until then, keep the Azure deployment script disabled.
