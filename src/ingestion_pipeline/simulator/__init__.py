"""
Batch Ingestion Simulator & Accuracy Benchmark Suite
-----------------------------------------------------
Provides high-scale benchmarking, accuracy validation, loss diagnostics,
and relational persistence (SQLite + CSV + HTML Dashboard) for document extraction.
"""

from .db import BenchmarkDatabase
from .runner import BatchSimulator, SimulationResult

__all__ = ["BenchmarkDatabase", "BatchSimulator", "SimulationResult"]
