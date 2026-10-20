# ADR 002: Use PaddleOCR as Primary OCR Engine

## Status
Accepted

## Context
The IndoLog platform requires OCR capabilities to process scanned documents (PDFs, images) for text extraction. Several OCR engines were evaluated, including Tesseract, Google Cloud Vision, AWS Textract, and PaddleOCR. The key requirements were:
- High accuracy for diverse document types (forms, tables, mixed layouts)
- Support for multiple languages (especially Indian languages)
- Ability to detect document structure (text blocks, tables, headings)
- Reasonable performance and cost for self-hosted deployment
- Good integration with Python-based pipeline

## Decision
We selected PaddleOCR (specifically PP-StructureV3) as the primary OCR engine for the following reasons:
1. **Superior Layout Analysis**: PaddleOCR's PP-StructureV3 model provides document structure detection (text, titles, tables, figures, etc.) out-of-the-box, which aligns with our requirement to tag content as blocks.
2. **High Accuracy**: Demonstrates state-of-the-art performance on various benchmarks, particularly for Chinese and English documents, with good support for other languages through multilingual models.
3. **Self-Hosted and Cost-Effective**: Being open-source, it avoids per-transaction costs of cloud APIs and allows us to control data privacy and compliance.
4. **Active Development**: Backed by Baidu with regular model updates and a growing ecosystem.
5. **Python Integration**: Official Python package and good documentation facilitate integration.
6. **Table Structure Recognition**: When combined with PaddleX, it provides robust table extraction capabilities.

We considered Tesseract but found its layout analysis capabilities limited without significant customization. Cloud APIs (Google Vision, AWS Textract) were ruled out due to recurring costs, data transfer requirements, and less flexibility for on-premises/hybrid deployments.

## Consequences
### Positive
- Enables sophisticated document understanding beyond plain text extraction
- Provides confidence scores for detected elements
- Supports GPU acceleration for improved throughput
- Allows fine-tuning through configuration options (thresholds, ratios, etc.)
- Includes table structure recognition via optional PaddleX integration
- Active community and regular model improvements

### Negative
- Larger disk and memory footprint compared to simpler OCR engines
- GPU dependency for optimal performance (CPU fallback is significantly slower)
- Model files require periodic updates to stay current with improvements
- Some advanced features (like formula recognition) may require additional model downloads
- Initial model download can be time-consuming (several hundred MBs)

## Related Documents
- [Technology Stack](./docs/architecture/tech-stack.md)
- [Service Documentation](./docs/services/ingestion-pipeline.md)
- [Configuration Reference](./src/ingestion_pipeline/config.py)