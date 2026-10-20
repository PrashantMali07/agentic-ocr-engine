# Six-to-nine-week implementation roadmap

Status: planning only, 19 October 2026. This roadmap expands [`AGENTIC_PIPELINE_IMPLEMENTATION_PLAN.MD`](../../AGENTIC_PIPELINE_IMPLEMENTATION_PLAN.MD). “Build spec” means the **code and interface specification**, not authorization to build a container image. No deployment, container build, model download, or production route switch is included.

After the 20 October Phase 00–04 review, follow [`PHASE_00_04_REMEDIATION.md`](PHASE_00_04_REMEDIATION.md) before marking those phases complete or starting Phase 05 comparisons. Its estimate covers repair work discovered during review and does not change the original roadmap's scope.

## Outcome

Create an isolated, end-to-end test lane that compares the current legacy PP-StructureV3/Tesseract pipeline with a Temporal-orchestrated, LangGraph-routed path and an optional-per-page VLM challenger. The runtime VLM experiment is part of the requested test lane; VLM promotion into the supported API is a later evidence-based decision. The supported `/extract/v1` legacy queue remains available throughout development.

FastAPI authenticates and admits a bounded job. Temporal owns durable job/page scheduling, retries, cancellation, and review wait. LangGraph makes bounded route and quality decisions using small metadata/results. The shared extraction service does the actual digital parsing, PaddleOCR, table reconstruction, and Tesseract fallback. A separate local VLM adapter produces candidates for comparison; neither VLM drafts nor OCR output become ground truth without image-based human review.

## Phase map and calendar

| Phase | Folder | Engineer time | Indicative calendar | Dependency | Deliverable |
| --- | --- | ---: | --- | --- | --- |
| 00 Contracts and baseline | [`phase_00_contracts`](phase_00_contracts/BUILD_SPEC.md) | 2–3 days | Week 1 | Existing code | Versioned request/page/result/decision contracts; fixture inventory. |
| 01 Shared extraction | [`phase_01_shared_extraction`](phase_01_shared_extraction/BUILD_SPEC.md) | 5–7 days | Weeks 1–2 | 00 | One bounded, complete-page extraction service used by legacy and test callers. |
| 02 Temporal test lane | [`phase_02_temporal`](phase_02_temporal/BUILD_SPEC.md) | 6–9 days | Weeks 2–4 | 01 | Durable isolated scheduling, status bridge, retries, cancellation, and review wait. |
| 03 LangGraph decisions | [`phase_03_langgraph`](phase_03_langgraph/BUILD_SPEC.md) | 4–6 days | Weeks 4–5 | 02 contracts | Route/audit decisions without a second production scheduler or image-filled checkpoints. |
| 04 VLM reference corpus | [`phase_04_vlm_labels`](phase_04_vlm_labels/BUILD_SPEC.md) | 3–5 days | Weeks 3–5, can overlap | 00 | Local VLM drafts and reviewer-ready, hash-bound corpus/evaluation flow. |
| 05 Runtime VLM challenger | [`phase_05_vlm_challenger`](phase_05_vlm_challenger/BUILD_SPEC.md) | 4–7 days | Weeks 5–7 | 03, 04 | Bounded test-only VLM candidate path and disagreement handling. |
| 06 Integration and acceptance | [`phase_06_validation`](phase_06_validation/BUILD_SPEC.md) | 5–8 days | Weeks 7–9 | 01–05 | Real API/Temporal/worker tests, differential quality report, and code-track sign-off. |

Total: **29–45 engineer-days**, about **6–9 working weeks for one engineer**. Calendar windows overlap where work is independent; adding their week labels would double count. A first local legacy + Temporal + LangGraph prototype follows phases 00–03 after about **17–25 engineer-days**. Estimates assume the repository's existing environment works, a local Temporal test server and GPU are available, and a permitted local VLM can be selected. Model choice, hardware faults, reviewer availability, and unexpected fixture failures can extend calendar time.

## Documents in each phase

- `BUILD_SPEC.md`: code boundaries, contracts, target files, invariants, and observable acceptance criteria.
- `EXECUTION_PLAN.md`: implementation order, focused tests, deliverables, and a completion checklist.
- `DESIGN_RATIONALE.md`: the reasoning behind the architecture, alternatives, risks, and decision points. It records design rationale, not private model reasoning.

Read the three files in a phase together. A phase is complete only when its exit checks and evidence exist; elapsed time alone does not close it.

## Cross-phase rules

1. Preserve the current authorized legacy route as the comparator. Experimental jobs use a distinct scheduler marker and cannot be claimed by the SQLite legacy worker.
2. Carry stable artifact IDs and small typed metadata across Temporal and LangGraph. Keep document bytes, rendered images, full OCR tokens, and VLM raw output in tenant-scoped bounded storage.
3. Use one-based page identifiers and explicit `complete`, `partial`, `needs_review`, `failed`, `cancelled`, and `timed_out` outcomes. Never convert a missing page or unknown confidence into success.
4. Keep exactly one lifecycle owner: Temporal coordinates experimental jobs; LangGraph returns decisions; FastAPI enforces identity and admission; activity results are idempotent.
5. Add focused tests with each phase. Phase 06 adds real integration and differential acceptance, rather than relying on mocks alone.
6. Keep private source documents and outputs inside ignored, access-restricted evidence locations; `test_asset` is input only. Do not commit transcripts, rendered pages, or OCR/VLM candidate text.

## Gates outside the 6–9-week code estimate

Independent reviewers must verify transcripts and field/table associations against images, and the business owner must set thresholds **before** scoring. GPU image build/smoke/scan/SBOM and representative soak are described in [`GPU_LOAD_TEST.MD`](../../GPU_LOAD_TEST.MD). Backups, alerts, and authenticated HTTPS ingress are described in [`PRODUCTION_OPERATION.MD`](../../PRODUCTION_OPERATION.MD). Those are later release/operations gates and are not performed by this roadmap. Phase 4 acceptance remains open until its separate owner/reviewer requirements are met.

## Change sets and final handoff

Land or review work in phase-sized changes with schema/migration notes. The Phase 06 handoff must contain: the contract versions; complete fixture matrix; Temporal restart/retry/cancel evidence; graph route trace samples without document text; frozen-reference quality comparison for legacy/OCR+graph/OCR+graph+VLM; queue/resource measurements; unresolved defects; and an explicit recommendation to keep or disable the VLM challenger. A production traffic switch requires separate approval and release gates.
