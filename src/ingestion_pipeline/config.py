import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

# Base directory for the project
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Default paths
DEFAULT_INPUT_DIR = os.getenv("INGESTION_INPUT_DIR", str(PROJECT_ROOT / "blob/input"))
DEFAULT_OUTPUT_DIR = os.getenv("INGESTION_OUTPUT_DIR", str(PROJECT_ROOT / "blob/output"))

# Backup policy for this installation. Local development may leave this false;
# production deployments must set it true and supply an encrypted backup job.
# This flag does not itself create or encrypt SQLite backups.
_encrypted_backups_value = os.getenv("ENCRYPTED_BACKUPS", "false").strip().lower()
if _encrypted_backups_value not in {"true", "false"}:
    raise ValueError("ENCRYPTED_BACKUPS must be true or false")
ENCRYPTED_BACKUPS = _encrypted_backups_value == "true"

# OCR/Paddle Configuration
PADDLE_USE_GPU = os.getenv("PADDLE_USE_GPU", "true").lower() == "true"

def get_int_env(key: str, default: str) -> int:
    """Get integer environment variable with default value.

    Args:
        key: Environment variable name
        default: Default value as string if env var is not set

    Returns:
        Integer value of the environment variable or default
    """
    return int(os.getenv(key, default))

# OCR rendering defaults (added for image size control)
# DPI for rendering PDF pages to raster images - lower values reduce size & VRAM.
OCR_DPI = get_int_env("OCR_DPI", "100")  # default 100 DPI (adjustable via env)
# OCR ENHANCEMENT: Whether to apply image enhancement (contrast, brightness) before OCR.
OCR_ENHANCE = os.getenv("OCR_ENHANCE", "true").lower() == "true"
# Maximum dimension (width or height) for a rendered image; PaddleOCR aborts >~4000px.
OCR_MAX_DIM = int(os.getenv("OCR_MAX_DIM", "4000"))
# Optional preprocessing for all images fed to OCR (including non-PDF image files)
# Cast to lower-precision dtype to shrink GPU memory usage. Accepted values: "uint8", "float16", "float32"
OCR_DTYPE = os.getenv("OCR_DTYPE", "uint8")
# Whether to convert images to grayscale before OCR (keeps 3-channel BGR shape for model compatibility)
OCR_GRAYSCALE = os.getenv("OCR_GRAYSCALE", "false").lower() == "true"

# SCANNED PDF CONFIGURATION KNOBS
LOW_DPI = get_int_env("LOW_DPI", "150")   # Default low DPI for faster processing
HIGH_DPI = get_int_env("HIGH_DPI", "300") # Default high DPI for better quality

LOW_MAX = get_int_env("LOW_MAX", "1000")  # Default max dimension for low DPI processing
HIGH_MAX = get_int_env("HIGH_MAX", "2000") # Default max dimension for high DPI processing

LARGE_PDF_SIZE_BYTES = get_int_env("LARGE_PDF_SIZE_BYTES", "10485760")  # Default 10MB
LARGE_PDF_PAGE_COUNT = get_int_env("LARGE_PDF_PAGE_COUNT", "50")       # Default 50 pages

# PPStructureV3 Pipeline Knobs
LAYOUT_NMS = os.getenv("LAYOUT_NMS", "true").lower() == "true"
LAYOUT_UNCLIP_RATIO = float(os.getenv("LAYOUT_UNCLIP_RATIO", "1.1"))
LAYOUT_MERGE_BBOXES_MODE = os.getenv("LAYOUT_MERGE_BBOXES_MODE", "large")
TEXT_DET_THRES = float(os.getenv("TEXT_DET_THRES", "0.2"))
TEXT_DET_BOX_THRES = float(os.getenv("TEXT_DET_BOX_THRES", "0.4"))
TEXT_DET_UNCLIP_RATIO = float(os.getenv("TEXT_DET_UNCLIP_RATIO", "1.8"))
TEXT_DET_LIMIT_SIDE_LEN_PDF = int(os.getenv("TEXT_DET_LIMIT_SIDE_LEN_PDF", "1280"))
TEXT_DET_LIMIT_SIDE_LEN_IMAGE = int(os.getenv("TEXT_DET_LIMIT_SIDE_LEN_IMAGE", "1600"))
TEXT_DET_LIMIT_TYPE = os.getenv("TEXT_DET_LIMIT_TYPE", "max")
TEXT_REC_SCORE_THRES = float(os.getenv("TEXT_REC_SCORE_THRES", "0.5"))
USE_DOC_ORIENTATION_CLASSIFY = os.getenv("USE_DOC_ORIENTATION_CLASSIFY", "true").lower() == "true"
USE_DOC_UNWRAPPING = os.getenv("USE_DOC_UNWRAPPING", "true").lower() == "true"
USE_TABLE_RECOGNITION = os.getenv("USE_TABLE_RECOGNITION", "true").lower() == "true"

# A client-supplied correlation ID is never used as a storage or graph key.
def generate_request_id() -> str:
    """Return a sortable identifier with 128 bits of cryptographic entropy."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3]
    return f"{timestamp}-{secrets.token_hex(16)}"


def feature_enabled(name: str) -> bool:
    """Dangerous compatibility features require an explicit deployment opt-in."""
    return os.getenv(name, "false").lower() == "true"

# Capture the exact time the application starts
# This acts as your "last updated" or "uptime start" timestamp
APP_START_TIME = datetime.now(timezone.utc).isoformat()
