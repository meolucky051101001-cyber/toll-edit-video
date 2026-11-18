"""Build a job-local Python/CUDA runtime without changing Colab's notebook Python."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def ensure_runtime(workdir: Path, spec: dict) -> Path:
    if os.name == "nt":
        raise RuntimeError("Cloud runtime bootstrap is not allowed on Windows")
    runtime_dir = (workdir / ".autodub_venv").absolute()
    python = runtime_dir / "bin" / "python"
    stamp = runtime_dir / ".runtime_spec.json"
    if python.is_file() and stamp.is_file() and json.loads(stamp.read_text()) == spec:
        return python
    bootstrap = workdir / ".autodub_bootstrap"
    env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(workdir / ".autodub_python"),
               UV_CACHE_DIR=str(workdir / ".autodub_uv_cache"))

    def run(argv, timeout=300):
        print("Runtime bootstrap: " + argv[0] + " " + argv[1], flush=True)
        result = subprocess.run(argv, cwd=workdir, env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if result.returncode:
            raise RuntimeError("Runtime bootstrap failed: " + (result.stdout + result.stderr)[-2500:])

    uv = bootstrap / "bin" / "uv"
    if not uv.is_file():
        run([sys.executable, "-m", "pip", "install", "--no-input", "--no-cache-dir", "--prefer-binary",
             "--target", str(bootstrap), "uv==" + spec["uv_version"]])
    run([str(uv), "venv", "--python", spec["python_version"], "--seed", str(runtime_dir)])
    run([str(python), "-m", "pip", "install", "--no-input", "--no-cache-dir", "--prefer-binary", "pip==25.3",
         "setuptools==75.6.0", "wheel==0.45.1", "numpy==" + spec.get("numpy_version", "1.26.4")])
    run([str(python), "-m", "pip", "install", "--no-input", "--no-cache-dir", "--prefer-binary",
         "--index-url", spec["torch_index_url"],
         *[name + "==" + version for name, version in spec["torch_packages"].items()]], timeout=600)
    stamp.write_text(json.dumps(spec, sort_keys=True), encoding="utf-8")
    return python
