"""Public compatibility metadata; contains no credentials or local path strings."""
import hashlib
from pathlib import Path

API_VERSION = 3
BUILD_ID = "2026-11-10.1"


def path_id(path):
    return hashlib.sha256(str(Path(path).resolve()).casefold().encode("utf-8")).hexdigest()[:20]


APP_ID = path_id(Path(__file__).resolve().parents[1])


def service_info(runtime_dir, storage_mode="legacy", queue_mode="local"):
    return {"api_version": API_VERSION, "build_id": BUILD_ID, "app_id": APP_ID,
            "runtime_id": path_id(runtime_dir), "storage_mode": storage_mode, "queue_mode": queue_mode,
            "capabilities": {"trash": True, "custom_materials": True}}
