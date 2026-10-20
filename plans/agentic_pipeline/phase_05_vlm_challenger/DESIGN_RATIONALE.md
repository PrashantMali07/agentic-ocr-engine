# Phase 05 design rationale

The VLM is a challenger, not a replacement, because document models can read a difficult region better while also hallucinating plausible text. Running it only on policy-selected pages controls GPU cost and gives a direct measurement of whether it improves reviewed outcomes. A separate candidate record makes every difference explainable and reversible.

Model output is untrusted input. A strict schema, bounded response, page/hash binding, and finite graph action set prevent a document or prompt response from choosing arbitrary tools or bypassing tenant boundaries. OCR/VLM agreement is evidence, not independent verification; both could make the same mistake on a blurry image.

GPU scheduling is a physical constraint, not something LangGraph or Temporal solves automatically. A single OCR GPU worker may coexist poorly with a VLM's model residency. The test lane must measure peak VRAM and p95 latency under the intended scheduling policy. If the model cannot fit or adds unacceptable delays, keep the challenger disabled while retaining the rest of the architecture.

The adapter abstracts a model-specific runner. This avoids binding the job contract or graph policy to one VLM provider and keeps cloud egress out of scope until explicitly approved.
