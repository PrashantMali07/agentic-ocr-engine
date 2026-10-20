#!/usr/bin/env python3
"""
High-Scale Document Ingestion Simulator & Benchmark CLI
-------------------------------------------------------
Runs batch extraction benchmarks across 100+ documents, validating:
1. Overall accuracy & OCR confidence distributions
2. Extraction percentage, word/character density
3. Table reconstruction completeness & empty-cell metrics
4. Loss & recovery diagnostics (missed edge words, isolated digits, etc.)
5. Full metadata & bounding-box persistence to SQLite DB & CSV
6. Interactive standalone HTML dashboard generation

Usage Examples:
    # Run simulation on 100 files (default: test_asset/Data + test_asset)
    uv run python scripts/run_simulator.py --limit 100

    # Quick test on first 10 documents
    uv run python scripts/run_simulator.py --limit 10

    # Run on all available test assets
    uv run python scripts/run_simulator.py --all

    # Run with custom DB and report directories
    uv run python scripts/run_simulator.py --db-path my_benchmark.db --output-dir my_reports
"""

import argparse
import sys
from pathlib import Path

# Add project root to sys.path (sys.path manipulation must precede the import)
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from src.ingestion_pipeline.simulator.runner import BatchSimulator  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="High-Scale Document Ingestion Simulator & Accuracy Benchmark"
    )
    parser.add_argument(
        "--input-dir", "-i",
        nargs="+",
        default=["test_asset/Data", "test_asset"],
        help="One or more directories or file paths to evaluate (default: test_asset/Data test_asset)"
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=100,
        help="Maximum number of files to process (default: 100)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all available documents in input directories without limit"
    )
    parser.add_argument(
        "--engine", "-e",
        choices=["langgraph", "decoupled"],
        default="langgraph",
        help="Extraction workflow engine (default: langgraph)"
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="benchmark_results.db",
        help="Path to SQLite database for persisting results (default: benchmark_results.db)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default="benchmark_reports",
        help="Directory to save CSV summaries and HTML dashboards (default: benchmark_reports)"
    )
    parser.add_argument(
        "--run-name",
        type=str,
        default="scale100",
        help="Name label for this benchmark run (default: scale100)"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-document console progress"
    )

    args = parser.parse_args()

    max_files = None if args.all else args.limit

    print("\n" + "=" * 90)
    print("DOCUMENT INGESTION BENCHMARK SIMULATOR")
    print("=" * 90)
    print(f"Target Input Sources: {args.input_dir}")
    print(f"File Limit:           {'ALL' if max_files is None else max_files}")
    print(f"Engine:               {args.engine}")
    print(f"SQLite DB:            {args.db_path}")
    print(f"Reports Directory:    {args.output_dir}")
    print("=" * 90 + "\n")

    simulator = BatchSimulator(
        db_path=args.db_path,
        reports_dir=args.output_dir,
        engine=args.engine,
        max_limit=max_files
    )

    result = simulator.run_simulation(
        input_sources=args.input_dir,
        run_name=args.run_name,
        verbose=not args.quiet
    )

    sys.exit(0 if result.failed_files == 0 else 1)


if __name__ == "__main__":
    main()
