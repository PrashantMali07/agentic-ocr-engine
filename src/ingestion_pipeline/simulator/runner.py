"""
Batch Simulator Execution Engine
--------------------------------
Executes large-scale document extraction benchmarks across 100+ documents,
computing accuracy, confidence distributions, table cell completeness,
and loss/recovery diagnostics, with persistence to SQLite, CSV, and HTML.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np

from ..graph.workflow import run_document_graph
from .db import BenchmarkDatabase
from .report import generate_html_dashboard

log = logging.getLogger("ingest.simulator")


@dataclass
class SimulationResult:
    run_id: str
    engine: str
    total_files: int
    successful_files: int
    failed_files: int
    total_pages: int
    total_duration_sec: float
    avg_duration_sec: float
    avg_confidence: float
    total_tables: int
    total_tokens_recovered: int
    db_path: Path
    summary_csv_path: Optional[Path] = None
    lost_tokens_csv_path: Optional[Path] = None
    html_report_path: Optional[Path] = None


class BatchSimulator:
    """Orchestrates high-volume document ingestion simulations."""

    SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".html", ".htm"}

    def __init__(
        self,
        db_path: Union[str, Path] = "benchmark_results.db",
        reports_dir: Union[str, Path] = "benchmark_reports",
        engine: str = "langgraph",
        max_limit: Optional[int] = None
    ):
        self.db = BenchmarkDatabase(Path(db_path))
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.max_limit = max_limit

    def collect_files(self, input_sources: List[Union[str, Path]]) -> List[Path]:
        """Collect all supported document files from input paths or directories."""
        files: List[Path] = []
        for src in input_sources:
            p = Path(src)
            if p.is_file() and p.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                files.append(p)
            elif p.is_dir():
                for ext in self.SUPPORTED_EXTENSIONS:
                    files.extend(p.glob(f"*{ext}"))
                    files.extend(p.glob(f"*{ext.upper()}"))

        # Deduplicate and sort deterministically by filename
        unique_files: List[Path] = []
        seen = set()
        for f in files:
            resolved = f.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique_files.append(f)

        unique_files.sort(key=lambda f: f.name.lower())

        if self.max_limit and self.max_limit > 0:
            unique_files = unique_files[:self.max_limit]

        return unique_files

    def run_simulation(
        self,
        input_sources: List[Union[str, Path]],
        run_name: Optional[str] = None,
        verbose: bool = True
    ) -> SimulationResult:
        """Execute the batch ingestion benchmark across all target documents."""
        files = self.collect_files(input_sources)
        if not files:
            raise ValueError(f"No supported document files found in: {input_sources}")

        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_id = f"run_{run_name or 'sim'}_{timestamp_str}"

        log.info("Starting simulation %s on %d files (engine=%s)...", run_id, len(files), self.engine)

        doc_records: List[Dict[str, Any]] = []
        all_lost_tokens: List[Dict[str, Any]] = []
        all_bboxes: List[Dict[str, Any]] = []
        all_tables: List[Dict[str, Any]] = []
        all_issues: List[Dict[str, Any]] = []

        t_sim_start = time.time()
        successful_count = 0
        failed_count = 0
        total_pages = 0

        for idx, file_path in enumerate(files, start=1):
            doc_id = f"{run_id}_doc_{idx:04d}"
            ext = file_path.suffix.lower().lstrip(".")
            file_size = file_path.stat().st_size if file_path.exists() else 0

            if verbose:
                print(f"[{idx:03d}/{len(files):03d}] Processing {file_path.name} ({ext.upper()})...", end="", flush=True)

            t0 = time.time()
            try:
                # Execute document extraction via selected engine
                if self.engine == "langgraph":
                    res = run_document_graph(file_path, thread_id=doc_id)
                elif self.engine == "decoupled":
                    from ..extraction.decoupled_pipeline import process_scanned_image_decoupled
                    res = process_scanned_image_decoupled(file_path)
                    res["file_path"] = str(file_path)
                    res["format"] = ext
                    res["pages"] = 1
                else:
                    raise ValueError(f"Unsupported simulation engine: {self.engine}")

                elapsed = round(time.time() - t0, 3)
                status = "success"
                err_msg = None
                successful_count += 1
            except Exception as e:
                elapsed = round(time.time() - t0, 3)
                status = "failed"
                err_msg = str(e)
                res = {}
                failed_count += 1
                log.warning("Extraction failed on %s: %s", file_path.name, e)

            # Analyze extracted properties
            clean_text = res.get("clean_text", "")
            html_content = res.get("html_content", "")
            pages = res.get("pages", 1)
            total_pages += pages
            is_digital = 1 if res.get("is_digital") else 0
            engine_used = res.get("engine_used", self.engine)
            deskew_angle = res.get("deskew_angle", 0.0)

            # Accuracy & Confidence Analysis
            raw_scores = res.get("raw_scores", [])
            raw_texts = res.get("raw_texts", [])
            raw_boxes = res.get("raw_boxes", [])
            audit = res.get("audit", {})

            if raw_scores:
                overall_conf = round(float(np.mean(raw_scores)), 4)
                min_conf = round(float(np.min(raw_scores)), 4)
                med_conf = round(float(np.median(raw_scores)), 4)
                high_conf_pct = round(sum(1 for s in raw_scores if s >= 0.90) / len(raw_scores) * 100.0, 1)
                med_conf_pct = round(sum(1 for s in raw_scores if 0.70 <= s < 0.90) / len(raw_scores) * 100.0, 1)
                low_conf_pct = round(sum(1 for s in raw_scores if s < 0.70) / len(raw_scores) * 100.0, 1)
            else:
                overall_conf = audit.get("overall_confidence", 1.0 if is_digital else 0.0)
                min_conf = overall_conf
                med_conf = overall_conf
                high_conf_pct = 100.0 if overall_conf >= 0.9 else 0.0
                med_conf_pct = 0.0
                low_conf_pct = 0.0 if overall_conf >= 0.9 else 100.0

            # Table & Cell Completeness Analysis
            tables = res.get("tables", [])
            total_cells = 0
            empty_cells = 0
            for t_idx, t in enumerate(tables):
                headers = t.get("headers", [])
                rows = t.get("rows", [])
                t_cells = len(headers)
                t_empty = sum(1 for h in headers if not str(h).strip())
                for r in rows:
                    t_cells += len(r)
                    t_empty += sum(1 for cell in r if not str(cell).strip())
                total_cells += t_cells
                empty_cells += t_empty

                all_tables.append({
                    "doc_id": doc_id,
                    "table_index": t_idx + 1,
                    "headers_json": json.dumps(headers),
                    "row_count": len(rows),
                    "col_count": len(headers),
                    "empty_cells": t_empty,
                    "avg_confidence": t.get("avg_confidence", 1.0),
                    "bbox_json": json.dumps(t.get("bbox", [])),
                    "html": t.get("html", "")
                })

            empty_cell_pct = round((empty_cells / total_cells * 100.0), 2) if total_cells > 0 else 0.0

            # Loss & Recovery Diagnostics
            recovered_tokens = res.get("recovered_tokens", [])
            for r in recovered_tokens:
                all_lost_tokens.append({
                    "doc_id": doc_id,
                    "token_text": r.get("text", ""),
                    "confidence": r.get("confidence", 0.0),
                    "box_json": json.dumps(r.get("box", [])),
                    "recovery_source": "tesseract_word",
                    "reason": "missed_by_primary_paddle_ocr"
                })

            # Bounding Box Metadata
            for t_text, t_score, t_box in zip(raw_texts, raw_scores, raw_boxes, strict=False):
                if len(t_box) == 4:
                    all_bboxes.append({
                        "doc_id": doc_id,
                        "element_type": "token",
                        "text": t_text,
                        "confidence": round(t_score, 3),
                        "x1": t_box[0],
                        "y1": t_box[1],
                        "x2": t_box[2],
                        "y2": t_box[3],
                        "polygon_json": json.dumps([])
                    })

            # Audit Issues
            issues_list = audit.get("issues", [])
            for iss in issues_list:
                all_issues.append({
                    "doc_id": doc_id,
                    "issue_type": iss.get("type", "general"),
                    "description": iss.get("description", str(iss)),
                    "severity": iss.get("severity", "warning")
                })

            doc_entry = {
                "doc_id": doc_id,
                "run_id": run_id,
                "file_path": str(file_path),
                "file_name": file_path.name,
                "file_format": ext,
                "file_size_bytes": file_size,
                "page_count": pages,
                "is_digital": is_digital,
                "engine_used": engine_used,
                "status": status,
                "error_message": err_msg,
                "duration_sec": elapsed,
                "deskew_angle": deskew_angle,
                "char_count": len(clean_text),
                "word_count": len(clean_text.split()),
                "token_count": len(raw_texts),
                "overall_confidence": overall_conf,
                "min_confidence": min_conf,
                "median_confidence": med_conf,
                "high_conf_token_pct": high_conf_pct,
                "med_conf_token_pct": med_conf_pct,
                "low_conf_token_pct": low_conf_pct,
                "table_count": len(tables),
                "total_table_cells": total_cells,
                "empty_table_cells": empty_cells,
                "empty_cell_pct": empty_cell_pct,
                "headings_count": res.get("headings_count", 0),
                "lost_tokens_count": len(recovered_tokens),
                "audit_is_valid": 1 if audit.get("is_valid", True) else 0,
                "audit_requires_hitl": 1 if audit.get("requires_hitl", False) else 0,
                "critical_issues_count": audit.get("critical_issues_count", 0),
                "warning_issues_count": audit.get("warning_issues_count", 0),
                "clean_text": clean_text,
                "html_content": html_content,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Real-time SQLite persistence per document
            self.db.save_document(doc_entry)
            doc_records.append(doc_entry)

            if verbose:
                rec_str = f", +{len(recovered_tokens)} recovered" if recovered_tokens else ""
                print(f" -> {status.upper()} in {elapsed:.2f}s (conf: {overall_conf*100:.1f}%, tables: {len(tables)}{rec_str})")

        # Save remaining relational records
        self.db.save_bounding_boxes(all_bboxes)
        self.db.save_tables(all_tables)
        self.db.save_lost_tokens(all_lost_tokens)
        self.db.save_audit_issues(all_issues)

        t_sim_total = round(time.time() - t_sim_start, 3)
        avg_duration = round(t_sim_total / len(files), 3) if files else 0.0
        avg_confidence = round(float(np.mean([d["overall_confidence"] for d in doc_records])), 4) if doc_records else 0.0

        run_summary = {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "engine": self.engine,
            "total_files": len(files),
            "successful_files": successful_count,
            "failed_files": failed_count,
            "total_pages": total_pages,
            "total_duration_sec": t_sim_total,
            "avg_duration_sec": avg_duration,
            "avg_confidence": avg_confidence,
            "total_tables": len(all_tables),
            "total_tokens_recovered": len(all_lost_tokens),
            "total_audit_issues": len(all_issues),
        }
        self.db.save_run(run_summary)

        # Generate CSV exports
        summary_csv = self.reports_dir / f"{run_id}_summary.csv"
        lost_tokens_csv = self.reports_dir / f"{run_id}_lost_tokens.csv"
        self.db.export_summary_csv(run_id, summary_csv)
        self.db.export_lost_tokens_csv(run_id, lost_tokens_csv)

        # Generate HTML Dashboard
        html_report = self.reports_dir / f"{run_id}_dashboard.html"
        generate_html_dashboard(run_summary, doc_records, all_lost_tokens, html_report)

        if verbose:
            print("\n" + "=" * 90)
            print(f"SIMULATION RUN COMPLETE: {run_id}")
            print("=" * 90)
            print(f"Total Documents:       {len(files)}")
            print(f"Successful:            {successful_count} ({(successful_count/len(files)*100):.1f}%)")
            print(f"Failed:                {failed_count}")
            print(f"Total Duration:        {t_sim_total:.2f}s (Avg {avg_duration:.2f}s / doc)")
            print(f"Mean Confidence:       {avg_confidence * 100:.2f}%")
            print(f"Reconstructed Tables:  {len(all_tables)}")
            print(f"Missed Tokens Caught:  {len(all_lost_tokens)} recovered by hybrid fallback")
            print(f"SQLite Database:       {self.db.db_path}")
            print(f"Summary CSV:           {summary_csv}")
            print(f"Lost Tokens CSV:       {lost_tokens_csv}")
            print(f"HTML Dashboard:        {html_report}")
            print("=" * 90 + "\n")

        return SimulationResult(
            run_id=run_id,
            engine=self.engine,
            total_files=len(files),
            successful_files=successful_count,
            failed_files=failed_count,
            total_pages=total_pages,
            total_duration_sec=t_sim_total,
            avg_duration_sec=avg_duration,
            avg_confidence=avg_confidence,
            total_tables=len(all_tables),
            total_tokens_recovered=len(all_lost_tokens),
            db_path=self.db.db_path,
            summary_csv_path=summary_csv,
            lost_tokens_csv_path=lost_tokens_csv,
            html_report_path=html_report
        )
