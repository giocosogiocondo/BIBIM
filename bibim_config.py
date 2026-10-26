"""Application identity and default paths; no extraction or validation rules."""

from pathlib import Path

VERSION = "3.4.7.1"
BUILD_VERSION = f"BIBIM-{VERSION}"
IMPORT_PREPROCESSOR = f"{BUILD_VERSION}-typed-IFC-import"
PROJECT_ROOT = Path(__file__).resolve().parent
MODEL_ROOT = PROJECT_ROOT / "data" / "models"
OUTPUT_ROOT = PROJECT_ROOT / ("outputs_v" + VERSION.replace(".", "_"))
CUSTOM_IDENTIFIERS = ("ifc_id", "ifc_class", "GlobalId", "Name")
