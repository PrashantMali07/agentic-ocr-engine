import argparse
import json
import logging
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DEFAULT_INPUT_DIR, PROJECT_ROOT, generate_request_id
from .extraction.manager import collect_input_files, process_email_attachments
from .processing.validation import validate_email

log = logging.getLogger("ingest.main")

# Add a filter that ensures every LogRecord has a 'request_id' attribute
class RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = ""
        return True

# Attach the filter to the root logger (affects all loggers)
logging.getLogger().addFilter(RequestIdFilter())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(request_id)s %(message)s",
    datefmt="%H:%M:%S",
)

# Set up rotating file handler for persistent log storage (5 MiB per file, keep 5 backups)
log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "ingestion.log"

file_handler = RotatingFileHandler(
    filename=log_file,
    maxBytes=5 * 1024 * 1024,  # 5 MiB
    backupCount=5,
    encoding="utf-8",
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(request_id)s %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(file_handler)


def email_paths(root: str, email_id: str) -> dict:
    """Return the fixed folder layout for one email."""
    base = Path(root) / email_id
    return {
        "base": base,
        "attachments": base / "attachments",
        "output": base / "output",
    }


def save_attachments(email_id: str, root: str, source_files: list[str]) -> Path:
    """Copy attachment files into <root>/<email_id>/attachments/."""
    paths = email_paths(root, email_id)
    paths["attachments"].mkdir(parents=True, exist_ok=True)

    expanded = collect_input_files(source_files)
    if not expanded:
        log.warning("No supported files found in provided attachment sources: %s", source_files)

    for raw in expanded:
        src = Path(raw)
        dest = paths["attachments"] / src.name
        shutil.copy2(src, dest)
        log.info("Saved attachment: %s", dest)

    return paths["attachments"]


def process_email(
    email_id: str,
    root: str,
    source_files: list[str] | None = None,
    visualize: bool = False,
    validate: bool = False,
    request_id: str | None = None,
) -> dict:
    """Full per-email flow: save attachments → run pipeline → optionally validate.

    ``request_id`` is a short sortable identifier used for all log lines.
    If omitted a new ID is generated via ``generate_request_id``.
    """
    if request_id is None:
        request_id = generate_request_id()
    # Use a LoggerAdapter so every log entry automatically includes the request_id
    log_adapter = logging.LoggerAdapter(log, {"request_id": request_id})

    paths = email_paths(root, email_id)

    if source_files:
        save_attachments(email_id, root, source_files)
    else:
        if not paths["attachments"].exists():
            raise FileNotFoundError(
                f"No attachments provided and {paths['attachments']} does not exist. "
                "Either pass --attachments or pre-populate the folder."
            )
        log_adapter.info("Using existing attachments in %s", paths["attachments"])

    files = collect_input_files([str(paths["attachments"])])
    if not files:
        raise ValueError(f"No supported attachment files found in {paths['attachments']}")

    paths["output"].mkdir(parents=True, exist_ok=True)
    # Pass the request_id down to the extraction stage
    record = process_email_attachments(
        files,
        str(paths["output"]),
        email_id,
        save_visualizations=visualize,
        request_id=request_id,
    )

    if validate:
        paths["output"] / f"{email_id}.json"
        val_report = validate_email(record, str(paths["attachments"]))
        val_path = paths["output"] / f"{email_id}_validation.json"
        val_path.write_text(json.dumps(val_report, indent=2))
        if val_report["needs_review"]:
            log_adapter.warning(
                "Validation flagged %d issue(s) -- see %s",
                len(val_report["flags"]),
                val_path,
            )

    log_adapter.info("Email %s complete: %s", email_id, paths["output"])
    return record


def main():
    parser = argparse.ArgumentParser(description="Process one email's attachments into its own folder.")
    parser.add_argument("--email-id", required=True, help="Unique identifier for this email.")
    parser.add_argument("--root", default=DEFAULT_INPUT_DIR, help="Root directory holding all per-email folders.")
    parser.add_argument(
        "--attachments",
        nargs="*",
        default=None,
        help="Source attachment file paths to copy in (optional if already in <email_id>/attachments/).",
    )
    parser.add_argument("--visualize", action="store_true", help="Also save marked-up page images.")
    parser.add_argument("--validate", action="store_true", help="Also validate extracted text against originals.")
    args = parser.parse_args()

    process_email(
        args.email_id,
        args.root,
        args.attachments,
        visualize=args.visualize,
        validate=args.validate,
    )

if __name__ == "__main__":
    main()
