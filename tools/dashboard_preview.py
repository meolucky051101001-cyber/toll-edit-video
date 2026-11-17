"""Local dashboard preview with isolated state and service startup disabled."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18088)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    temporary = tempfile.TemporaryDirectory(prefix="dashboard_preview_") if args.runtime is None else None
    runtime = args.runtime or Path(temporary.name)
    for key, directory in {"AUTODUB_WORKSPACE": "workspace", "AUTODUB_INPUT_DIR": "input", "AUTODUB_OUTPUT_DIR": "output",
                            "AUTODUB_DATA_DIR": "data", "AUTODUB_MODEL_CACHE": "models", "AUTODUB_SHARED_ASSETS_DIR": "assets"}.items():
        path = runtime / directory
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ.update(AUTODUB_TESTING="1", AUTODUB_LOCAL_TOKEN="isolated-dashboard-preview-token", AUTODUB_PORT=str(args.port))
    sys.path.insert(0, str(root / "backend"))
    import uvicorn
    print(json.dumps({"url": f"http://127.0.0.1:{args.port}", "runtime": str(runtime), "isolated": True}), flush=True)
    try:
        uvicorn.run("main:app", host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    main()
