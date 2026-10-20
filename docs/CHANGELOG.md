# Changelog

All notable changes to the PaddleOCR service will be documented in this file.

## [Unreleased]
### Added
- Initial documentation scaffolding
- Architecture overview with C4 diagrams
- Service-specific documentation
- OpenAPI 3.1 specification
- Database schema documentation
- Runbooks for local setup, deployment, and troubleshooting
- Architecture Decision Records (ADRs)

## [0.2.0] - 2026-09-30
### Added
- PaddleOCR service for IndoLog platform
- Async job processing with semaphore-based concurrency control
- SQLite-backed job store
- Support for multiple document formats (PDF, images, Office files, HTML)
- Language detection via langdetect
- REST API with synchronous and asynchronous endpoints
- Docker Compose configurations for CPU and GPU
- Azure Container Apps deployment scripts
- Initial version of the ingestion pipeline
