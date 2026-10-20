"""
SQLite Persistence Layer for Benchmark Simulator
------------------------------------------------
Stores runs, document metrics, token-level bounding boxes,
reconstructed tables, loss/recovery diagnostics, and audit issues.
"""

import csv
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


def _spreadsheet_safe(value):
    """Neutralize formula prefixes in exported CSV, leaving stored values intact."""
    if isinstance(value, str) and value and (value[0] in "=+-@\t\r\n" or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


class BenchmarkDatabase:
    """Manages SQLite storage and CSV export for benchmark simulator runs."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for high concurrency & write speed
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    total_files INTEGER DEFAULT 0,
                    successful_files INTEGER DEFAULT 0,
                    failed_files INTEGER DEFAULT 0,
                    total_pages INTEGER DEFAULT 0,
                    total_duration_sec REAL DEFAULT 0.0,
                    avg_duration_sec REAL DEFAULT 0.0,
                    avg_confidence REAL DEFAULT 0.0,
                    total_tables INTEGER DEFAULT 0,
                    total_tokens_recovered INTEGER DEFAULT 0,
                    total_audit_issues INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_format TEXT NOT NULL,
                    file_size_bytes INTEGER DEFAULT 0,
                    page_count INTEGER DEFAULT 1,
                    is_digital INTEGER DEFAULT 0,
                    engine_used TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    duration_sec REAL DEFAULT 0.0,
                    deskew_angle REAL DEFAULT 0.0,
                    char_count INTEGER DEFAULT 0,
                    word_count INTEGER DEFAULT 0,
                    token_count INTEGER DEFAULT 0,
                    overall_confidence REAL DEFAULT 0.0,
                    min_confidence REAL DEFAULT 0.0,
                    median_confidence REAL DEFAULT 0.0,
                    high_conf_token_pct REAL DEFAULT 0.0,
                    med_conf_token_pct REAL DEFAULT 0.0,
                    low_conf_token_pct REAL DEFAULT 0.0,
                    table_count INTEGER DEFAULT 0,
                    total_table_cells INTEGER DEFAULT 0,
                    empty_table_cells INTEGER DEFAULT 0,
                    empty_cell_pct REAL DEFAULT 0.0,
                    headings_count INTEGER DEFAULT 0,
                    lost_tokens_count INTEGER DEFAULT 0,
                    audit_is_valid INTEGER DEFAULT 1,
                    audit_requires_hitl INTEGER DEFAULT 0,
                    critical_issues_count INTEGER DEFAULT 0,
                    warning_issues_count INTEGER DEFAULT 0,
                    clean_text TEXT,
                    html_content TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS bounding_boxes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    element_type TEXT NOT NULL, -- 'token', 'heading', 'table', 'recovered'
                    text TEXT,
                    confidence REAL,
                    x1 INTEGER,
                    y1 INTEGER,
                    x2 INTEGER,
                    y2 INTEGER,
                    polygon_json TEXT,
                    FOREIGN KEY (doc_id) REFERENCES documents (doc_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS tables_extracted (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    table_index INTEGER NOT NULL,
                    headers_json TEXT,
                    row_count INTEGER DEFAULT 0,
                    col_count INTEGER DEFAULT 0,
                    empty_cells INTEGER DEFAULT 0,
                    avg_confidence REAL DEFAULT 1.0,
                    bbox_json TEXT,
                    html TEXT,
                    FOREIGN KEY (doc_id) REFERENCES documents (doc_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lost_and_recovered_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    token_text TEXT NOT NULL,
                    confidence REAL,
                    box_json TEXT,
                    recovery_source TEXT,
                    reason TEXT,
                    FOREIGN KEY (doc_id) REFERENCES documents (doc_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    issue_type TEXT,
                    description TEXT,
                    severity TEXT,
                    FOREIGN KEY (doc_id) REFERENCES documents (doc_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_doc_run_id ON documents (run_id);
                CREATE INDEX IF NOT EXISTS idx_doc_filename ON documents (file_name);
                CREATE INDEX IF NOT EXISTS idx_bbox_doc_id ON bounding_boxes (doc_id);
                CREATE INDEX IF NOT EXISTS idx_tables_doc_id ON tables_extracted (doc_id);
                CREATE INDEX IF NOT EXISTS idx_lost_doc_id ON lost_and_recovered_tokens (doc_id);
                CREATE INDEX IF NOT EXISTS idx_issues_doc_id ON audit_issues (doc_id);
            """)

    def save_run(self, run_dict: Dict[str, Any]) -> None:
        """Insert or replace a run entry."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO runs (
                    run_id, created_at, engine, total_files, successful_files, failed_files,
                    total_pages, total_duration_sec, avg_duration_sec, avg_confidence,
                    total_tables, total_tokens_recovered, total_audit_issues
                ) VALUES (
                    :run_id, :created_at, :engine, :total_files, :successful_files, :failed_files,
                    :total_pages, :total_duration_sec, :avg_duration_sec, :avg_confidence,
                    :total_tables, :total_tokens_recovered, :total_audit_issues
                )
            """, run_dict)

    def save_document(self, doc: Dict[str, Any]) -> None:
        """Insert or replace a document record."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO documents (
                    doc_id, run_id, file_path, file_name, file_format, file_size_bytes,
                    page_count, is_digital, engine_used, status, error_message, duration_sec,
                    deskew_angle, char_count, word_count, token_count, overall_confidence,
                    min_confidence, median_confidence, high_conf_token_pct, med_conf_token_pct,
                    low_conf_token_pct, table_count, total_table_cells, empty_table_cells,
                    empty_cell_pct, headings_count, lost_tokens_count, audit_is_valid,
                    audit_requires_hitl, critical_issues_count, warning_issues_count,
                    clean_text, html_content, created_at
                ) VALUES (
                    :doc_id, :run_id, :file_path, :file_name, :file_format, :file_size_bytes,
                    :page_count, :is_digital, :engine_used, :status, :error_message, :duration_sec,
                    :deskew_angle, :char_count, :word_count, :token_count, :overall_confidence,
                    :min_confidence, :median_confidence, :high_conf_token_pct, :med_conf_token_pct,
                    :low_conf_token_pct, :table_count, :total_table_cells, :empty_table_cells,
                    :empty_cell_pct, :headings_count, :lost_tokens_count, :audit_is_valid,
                    :audit_requires_hitl, :critical_issues_count, :warning_issues_count,
                    :clean_text, :html_content, :created_at
                )
            """, doc)

    def save_bounding_boxes(self, boxes: List[Dict[str, Any]]) -> None:
        """Batch insert token and layout bounding boxes."""
        if not boxes:
            return
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO bounding_boxes (
                    doc_id, element_type, text, confidence, x1, y1, x2, y2, polygon_json
                ) VALUES (
                    :doc_id, :element_type, :text, :confidence, :x1, :y1, :x2, :y2, :polygon_json
                )
            """, boxes)

    def save_tables(self, tables: List[Dict[str, Any]]) -> None:
        """Batch insert reconstructed tables."""
        if not tables:
            return
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO tables_extracted (
                    doc_id, table_index, headers_json, row_count, col_count,
                    empty_cells, avg_confidence, bbox_json, html
                ) VALUES (
                    :doc_id, :table_index, :headers_json, :row_count, :col_count,
                    :empty_cells, :avg_confidence, :bbox_json, :html
                )
            """, tables)

    def save_lost_tokens(self, tokens: List[Dict[str, Any]]) -> None:
        """Batch insert lost/recovered token diagnostics."""
        if not tokens:
            return
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO lost_and_recovered_tokens (
                    doc_id, token_text, confidence, box_json, recovery_source, reason
                ) VALUES (
                    :doc_id, :token_text, :confidence, :box_json, :recovery_source, :reason
                )
            """, tokens)

    def save_audit_issues(self, issues: List[Dict[str, Any]]) -> None:
        """Batch insert audit issues."""
        if not issues:
            return
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO audit_issues (
                    doc_id, issue_type, description, severity
                ) VALUES (
                    :doc_id, :issue_type, :description, :severity
                )
            """, issues)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_documents_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM documents WHERE run_id = ? ORDER BY file_name ASC", (run_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_lost_tokens_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT l.*, d.file_name, d.file_format
                FROM lost_and_recovered_tokens l
                JOIN documents d ON l.doc_id = d.doc_id
                WHERE d.run_id = ?
                ORDER BY d.file_name ASC
            """, (run_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_tables_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT t.*, d.file_name
                FROM tables_extracted t
                JOIN documents d ON t.doc_id = d.doc_id
                WHERE d.run_id = ?
                ORDER BY d.file_name ASC, t.table_index ASC
            """, (run_id,))
            return [dict(r) for r in cursor.fetchall()]

    def export_summary_csv(self, run_id: str, csv_path: Path) -> Path:
        """Export comprehensive document summary to CSV."""
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        docs = self.get_documents_for_run(run_id)
        if not docs:
            return csv_path

        fieldnames = [
            "file_name", "file_format", "page_count", "is_digital", "engine_used",
            "status", "duration_sec", "overall_confidence", "min_confidence",
            "high_conf_token_pct", "char_count", "word_count", "table_count",
            "total_table_cells", "empty_table_cells", "empty_cell_pct",
            "lost_tokens_count", "audit_is_valid", "audit_requires_hitl",
            "critical_issues_count", "deskew_angle", "error_message"
        ]

        with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for d in docs:
                row = {k: d.get(k) for k in fieldnames}
                writer.writerow({k: _spreadsheet_safe(v) for k, v in row.items()})
        return csv_path

    def export_lost_tokens_csv(self, run_id: str, csv_path: Path) -> Path:
        """Export lost & recovered tokens report to CSV."""
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        tokens = self.get_lost_tokens_for_run(run_id)

        fieldnames = ["file_name", "token_text", "confidence", "box_json", "recovery_source", "reason"]
        with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in tokens:
                writer.writerow({k: _spreadsheet_safe(t.get(k)) for k in fieldnames})
        return csv_path
