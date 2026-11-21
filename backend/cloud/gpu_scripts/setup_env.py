"""
setup_env.py - Script kiểm tra phần cứng, cài đặt idempotent môi trường GPU Cloud
theo version cố định trong cloud_env_manifest.json, và xác thực bundle/video tải lên.

Chạy trên VM Colab (Linux /content) hoặc trong kiểm thử cô lập với --allow-local-test.
Không bao giờ được tự chạy để sửa backend/venv cục bộ trên máy Windows của người dùng.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


EXIT_OK = 0
EXIT_ENV_SETUP_FAILED = 1
EXIT_GPU_MISMATCH = 10
EXIT_DISK_INSUFFICIENT = 11
EXIT_NETWORK_ERROR = 12
EXIT_PACKAGE_MISSING = 13
EXIT_MODEL_LOAD_FAILED = 14
EXIT_UPLOAD_VERIFICATION_FAILED = 15

RESULT_PREFIX = "SETUP_ENV_RESULT_JSON:"
_ORIGINAL_SUBPROCESS_RUN = subprocess.run

_NETWORK_ERROR_MARKERS = (
    "newconnectionerror",
    "nameresolutionerror",
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "readtimeouterror",
    "connecttimeouterror",
    "connectionerror",
    "connectionreseterror",
    "proxyerror",
    "max retries exceeded",
    "nodename nor servname provided",
    "could not find a version that satisfies",
    "warning: retrying",
    "timed out",
)


def _sha256_file(path_obj: Path) -> str:
    h = hashlib.sha256()
    with open(path_obj, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_production_local_env() -> Tuple[bool, str]:
    """Detect Windows local workstation or Tool V1 backend/venv to avoid mutating it."""
    if os.name == "nt":
        return True, "Refusing to run Cloud setup_env on Windows host without --allow-local-test."
    prefix_path = Path(sys.prefix).resolve()
    parts_lower = [p.lower() for p in prefix_path.parts]
    if "venv" in parts_lower and "backend" in parts_lower:
        return True, f"Refusing to mutate local backend venv ({prefix_path}) without --allow-local-test."
    return False, ""


def _resolve_in_workdir(workdir: Path, raw_path: str) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else (workdir / p)


def _detect_gpu() -> Dict[str, Any]:
    """Detect NVIDIA GPU via nvidia-smi and PyTorch CUDA (without failing on missing binaries)."""
    info: Dict[str, Any] = {
        "nvidia_smi_ok": False,
        "detected_names": [],
        "detected_name": None,
        "driver_version": None,
        "memory_mb": None,
        "torch_installed": False,
        "torch_version": None,
        "torch_cuda_version": None,
        "cuda_available": False,
        "device_count": 0,
    }
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            proc = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=name,memory.total,driver_version",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                names: List[str] = []
                for line in proc.stdout.strip().splitlines():
                    cols = [c.strip() for c in line.split(",")]
                    if cols and cols[0]:
                        names.append(cols[0])
                        if info["memory_mb"] is None and len(cols) >= 2:
                            info["memory_mb"] = cols[1]
                        if info["driver_version"] is None and len(cols) >= 3:
                            info["driver_version"] = cols[2]
                if names:
                    info["nvidia_smi_ok"] = True
                    info["detected_names"] = names
                    info["detected_name"] = names[0]
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        torch_mod = importlib.import_module("torch")
        info["torch_installed"] = True
        info["torch_version"] = getattr(torch_mod, "__version__", None)
        version_ns = getattr(torch_mod, "version", None)
        info["torch_cuda_version"] = getattr(version_ns, "cuda", None) if version_ns else None
        cuda_ns = getattr(torch_mod, "cuda", None)
        if cuda_ns and callable(getattr(cuda_ns, "is_available", None)) and cuda_ns.is_available():
            info["cuda_available"] = True
            count = int(cuda_ns.device_count()) if callable(getattr(cuda_ns, "device_count", None)) else 1
            info["device_count"] = count
            if count > 0 and callable(getattr(cuda_ns, "get_device_name", None)):
                torch_name = str(cuda_ns.get_device_name(0))
                if not info["detected_name"]:
                    info["detected_name"] = torch_name
                if torch_name and torch_name not in info["detected_names"]:
                    info["detected_names"].append(torch_name)
    except Exception:
        pass

    return info


def _gpu_matches_expected(
    expected_gpu: str,
    gpu_info: Dict[str, Any],
    family_patterns: Dict[str, List[str]],
    *,
    allow_any_gpu: bool,
    require_torch_cuda: bool,
) -> Tuple[bool, str]:
    if allow_any_gpu:
        return True, "allow-any-gpu enabled"
    exp = (expected_gpu or "").strip().upper()
    if not exp or exp == "CPU":
        return True, f"non-GPU target '{exp or 'CPU'}'"

    detected = gpu_info.get("detected_name") or ""
    has_hw_gpu = bool(gpu_info.get("nvidia_smi_ok") or gpu_info.get("cuda_available"))
    if not has_hw_gpu or not detected:
        return False, f"Expected GPU '{exp}', but no CUDA/NVIDIA GPU was detected."

    if require_torch_cuda and gpu_info.get("torch_installed") and not gpu_info.get("cuda_available"):
        return (
            False,
            f"GPU '{detected}' detected by driver, but torch.cuda.is_available() is False (PyTorch/CUDA mismatch).",
        )

    patterns = [p.lower() for p in family_patterns.get(exp, [exp.lower()])]
    detected_lower = detected.lower()
    if any(pat in detected_lower for pat in patterns):
        return True, f"Detected GPU '{detected}' matches expected family '{exp}'."
    return False, f"GPU mismatch: expected '{exp}' ({patterns}), detected '{detected}'."


def _classify_pip_failure(output_text: str) -> Tuple[int, str]:
    lowered = (output_text or "").lower()
    if any(marker in lowered for marker in _NETWORK_ERROR_MARKERS):
        return EXIT_NETWORK_ERROR, "ENV_NETWORK_ERROR"
    return EXIT_PACKAGE_MISSING, "ENV_PACKAGE_MISSING"


def _normalize_version(ver: str) -> str:
    return (ver or "").split("+", 1)[0].strip()


def _verify_packages_and_binaries(
    manifest: Dict[str, Any],
    workdir: Path,
) -> Tuple[bool, int, str, str, Dict[str, Any]]:
    """Verify wheel checksums, required binaries, pinned Python packages, and imports."""
    versions: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "packages": {},
        "wheels": {},
        "binaries": {},
    }

    for wheel_spec in manifest.get("wheels", []):
        filename = wheel_spec["filename"]
        wheel_path = workdir / filename
        if not wheel_path.is_file():
            return (
                False,
                EXIT_PACKAGE_MISSING,
                "ENV_PACKAGE_MISSING",
                f"Required wheel file missing: {filename}",
                versions,
            )
        actual_sha = _sha256_file(wheel_path)
        expected_sha = wheel_spec.get("sha256", "")
        if expected_sha and actual_sha != expected_sha:
            return (
                False,
                EXIT_PACKAGE_MISSING,
                "ENV_PACKAGE_MISSING",
                f"Wheel SHA256 mismatch for {filename}: expected {expected_sha[:12]}, got {actual_sha[:12]}",
                versions,
            )
        pkg_name = wheel_spec["package"]
        imp_name = wheel_spec["import_name"]
        try:
            installed_ver = importlib.metadata.version(pkg_name)
            importlib.import_module(imp_name)
        except Exception as exc:
            return (
                False,
                EXIT_PACKAGE_MISSING,
                "ENV_PACKAGE_MISSING",
                f"Wheel package '{pkg_name}' ({imp_name}) not ready: {exc}",
                versions,
            )
        versions["wheels"][pkg_name] = installed_ver

    for bin_name in manifest.get("required_binaries", []):
        found_bin = shutil.which(bin_name)
        if not found_bin:
            return (
                False,
                EXIT_PACKAGE_MISSING,
                "ENV_PACKAGE_MISSING",
                f"Required system binary not found in PATH: {bin_name}",
                versions,
            )
        versions["binaries"][bin_name] = found_bin

    for pkg_spec in manifest.get("pinned_packages", []):
        pkg_name = pkg_spec["package"]
        expected_ver = pkg_spec.get("version")
        imp_name = pkg_spec["import_name"]
        try:
            installed_ver = importlib.metadata.version(pkg_name)
        except importlib.metadata.PackageNotFoundError:
            return (
                False,
                EXIT_PACKAGE_MISSING,
                "ENV_PACKAGE_MISSING",
                f"Required package '{pkg_name}' is not installed.",
                versions,
            )
        if expected_ver and _normalize_version(installed_ver) != _normalize_version(expected_ver):
            return (
                False,
                EXIT_PACKAGE_MISSING,
                "ENV_PACKAGE_MISSING",
                f"Package version mismatch for '{pkg_name}': expected {expected_ver}, got {installed_ver}",
                versions,
            )
        try:
            importlib.import_module(imp_name)
        except Exception as exc:
            return (
                False,
                EXIT_PACKAGE_MISSING,
                "ENV_PACKAGE_MISSING",
                f"Failed to import '{imp_name}' ({pkg_name}): {exc}",
                versions,
            )
        versions["packages"][pkg_name] = installed_ver

    return True, EXIT_OK, "", "All packages and binaries verified.", versions


def _install_dependencies(
    manifest: Dict[str, Any],
    workdir: Path,
    *,
    pip_timeout: int,
) -> Tuple[bool, int, str, str]:
    req_rel = manifest.get("requirements_file", "requirements_cloud.txt")
    req_path = _resolve_in_workdir(workdir, req_rel)
    if not req_path.is_file():
        return False, EXIT_PACKAGE_MISSING, "ENV_PACKAGE_MISSING", f"Requirements file missing: {req_path}"

    # Verify wheel SHA256 before pip install
    wheel_paths: List[Path] = []
    for wheel_spec in manifest.get("wheels", []):
        wp = workdir / wheel_spec["filename"]
        if not wp.is_file():
            return False, EXIT_PACKAGE_MISSING, "ENV_PACKAGE_MISSING", f"Wheel missing before install: {wp.name}"
        if wheel_spec.get("sha256") and _sha256_file(wp) != wheel_spec["sha256"]:
            return False, EXIT_PACKAGE_MISSING, "ENV_PACKAGE_MISSING", f"Wheel SHA256 mismatch before install: {wp.name}"
        wheel_paths.append(wp)

    # Fast path on Linux Colab VMs: parallel wheel download & extraction via bootstrapped uv
    uv_bin = workdir / ".autodub_bootstrap" / "bin" / "uv"
    if os.name != "nt" and uv_bin.is_file():
        uv_env = dict(os.environ, UV_HTTP_TIMEOUT=str(max(30, int(pip_timeout))))
        try:
            uv_proc = subprocess.run(
                [
                    str(uv_bin),
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--no-cache",
                    "--no-build-isolation",
                    *(str(wp) for wp in wheel_paths),
                    "-r",
                    str(req_path),
                ],
                cwd=str(workdir),
                env=uv_env,
                capture_output=True,
                text=True,
                timeout=max(pip_timeout * 2, 120),
                shell=False,
            )
            if uv_proc.returncode == 0:
                return True, EXIT_OK, "", "Dependencies installed cleanly via uv."
        except (OSError, subprocess.SubprocessError):
            pass

    for wp in wheel_paths:
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--no-cache-dir",
                    "--prefer-binary",
                    "--timeout",
                    str(pip_timeout),
                    str(wp),
                ],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=max(pip_timeout + 30, 60),
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return False, EXIT_NETWORK_ERROR, "ENV_NETWORK_ERROR", f"Timed out installing wheel {wp.name} after {pip_timeout}s."
        except OSError as exc:
            return False, EXIT_PACKAGE_MISSING, "ENV_PACKAGE_MISSING", f"Failed executing pip for wheel {wp.name}: {exc}"

        if proc.returncode != 0:
            combined = f"{proc.stdout}\n{proc.stderr}"
            rc, code = _classify_pip_failure(combined)
            return False, rc, code, f"Wheel install failed ({wp.name}): {combined[-400:]}"

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-input",
                "--no-cache-dir",
                "--prefer-binary",
                "--timeout",
                str(pip_timeout),
                "-r",
                str(req_path),
                "--no-build-isolation",
            ],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=max(pip_timeout * 2, 120),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, EXIT_NETWORK_ERROR, "ENV_NETWORK_ERROR", f"Timed out installing requirements after {pip_timeout}s."
    except OSError as exc:
        return False, EXIT_PACKAGE_MISSING, "ENV_PACKAGE_MISSING", f"Failed executing pip install: {exc}"

    if proc.returncode != 0:
        combined = f"{proc.stdout}\n{proc.stderr}"
        rc, code = _classify_pip_failure(combined)
        return False, rc, code, f"pip install failed: {combined[-500:]}"

    return True, EXIT_OK, "", "Dependencies installed cleanly."


def _clear_subtitle_layout_caches() -> None:
    for mod_name in ("subtitle_layout", "backend.subtitle_layout"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for fn_name in ("_installed_fonts", "subtitle_measure"):
            fn = getattr(mod, fn_name, None)
            clear_fn = getattr(fn, "cache_clear", None)
            if callable(clear_fn):
                try:
                    clear_fn()
                except Exception:
                    pass


def _stage_bundled_subtitle_fonts(workdir: Path, *, allow_local_test: bool = False) -> int:
    """Stage bundled host fonts (e.g. fonts/arial*.ttf) into workdir & Linux system font dir."""
    fonts_dir = workdir / "fonts"
    if not fonts_dir.is_dir():
        return 0
    staged = 0
    font_files = [
        p for p in sorted(fonts_dir.iterdir())
        if p.is_file() and p.suffix.lower() in {".ttf", ".otf", ".ttc"}
    ]
    if not font_files:
        return 0

    system_font_dir: Optional[Path] = None
    if os.name != "nt" and not allow_local_test:
        cand_sys = Path("/usr/local/share/fonts/autodub")
        try:
            cand_sys.mkdir(parents=True, exist_ok=True)
            system_font_dir = cand_sys
        except OSError:
            system_font_dir = None

    for font_file in font_files:
        target_work = workdir / font_file.name
        if not target_work.exists():
            try:
                os.symlink(font_file, target_work)
            except OSError:
                try:
                    shutil.copy2(font_file, target_work)
                except OSError:
                    pass
        if system_font_dir is not None:
            target_sys = system_font_dir / font_file.name
            try:
                if not target_sys.exists() or target_sys.stat().st_size != font_file.stat().st_size:
                    shutil.copy2(font_file, target_sys)
            except OSError:
                pass
        staged += 1

    if system_font_dir is not None:
        fc_cache = shutil.which("fc-cache")
        if fc_cache:
            try:
                subprocess.run(
                    [fc_cache, "-f", str(system_font_dir)],
                    capture_output=True, text=True, timeout=30, shell=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    _clear_subtitle_layout_caches()
    return staged


def _has_linux_subtitle_font_pair(workdir: Optional[Path] = None) -> bool:
    font_roots = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
    if workdir is not None:
        font_roots.extend([workdir, workdir / "fonts"])
    found_names: set[str] = set()
    for root in font_roots:
        if not root.is_dir():
            continue
        try:
            iterator = root.iterdir() if root == workdir else root.rglob("*")
            for p in iterator:
                if p.is_file() and p.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                    found_names.add(p.name.lower())
        except OSError:
            continue
    has_arial = "arial.ttf" in found_names and "arialbd.ttf" in found_names
    has_liberation = (
        "liberationsans-regular.ttf" in found_names and "liberationsans-bold.ttf" in found_names
    )
    has_dejavu = "dejavusans.ttf" in found_names and "dejavusans-bold.ttf" in found_names
    return has_arial or has_liberation or has_dejavu


def _ensure_linux_subtitle_fonts(*, allow_local_test: bool, workdir: Optional[Path] = None) -> Tuple[bool, str]:
    """Install Liberation/DejaVu TrueType fonts on bare Linux Colab VMs for ASS blur & Vietnamese rendering."""
    if allow_local_test or os.name == "nt" or subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
        return True, "skipped on local/test host"
    if _has_linux_subtitle_font_pair(workdir):
        return True, "Linux subtitle fonts already present"
    apt_bin = shutil.which("apt-get")
    euid = getattr(os, "geteuid", lambda: 1)()
    if not apt_bin or euid != 0:
        return False, "Missing Liberation/DejaVu fonts and cannot run apt-get as root"
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    try:
        upd = subprocess.run([apt_bin, "update"], env=env, capture_output=True, text=True, timeout=60, shell=False)
        if upd.returncode != 0:
            return False, f"apt-get update failed: {(upd.stderr or upd.stdout)[-300:]}"
        inst = subprocess.run(
            [apt_bin, "install", "-y", "--no-install-recommends", "fonts-liberation", "fonts-dejavu-core", "fontconfig"],
            env=env, capture_output=True, text=True, timeout=90, shell=False,
        )
        if inst.returncode != 0:
            return False, f"apt-get install fonts failed: {(inst.stderr or inst.stdout)[-300:]}"
        fc_cache = shutil.which("fc-cache")
        if fc_cache:
            subprocess.run([fc_cache, "-f"], env=env, capture_output=True, text=True, timeout=30, shell=False)
        _clear_subtitle_layout_caches()
        return True, "Installed fonts-liberation and fonts-dejavu-core"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Subtitle font installation error: {exc}"


def _verify_models_readiness(manifest: Dict[str, Any], workdir: Path) -> Tuple[bool, str]:
    """Check model runtime APIs / revisions and any declared local model weight checkpoints."""
    models = manifest.get("models") or {}

    whisper_cfg = models.get("whisper")
    if whisper_cfg:
        try:
            whisper_mod = importlib.import_module("whisper")
            avail_fn = getattr(whisper_mod, "available_models", None)
            load_fn = getattr(whisper_mod, "load_model", None)
            if not callable(load_fn):
                return False, "whisper.load_model is not callable."
            model_id = whisper_cfg.get("model_id")
            if callable(avail_fn) and model_id and model_id not in avail_fn():
                return False, f"Pinned Whisper model '{model_id}' unavailable in installed whisper runtime."
        except Exception as exc:
            return False, f"Whisper model readiness check failed: {exc}"

    if models.get("demucs"):
        try:
            demucs_mod = importlib.import_module("demucs")
            if demucs_mod is None:
                return False, "demucs module unavailable."
        except Exception as exc:
            return False, f"Demucs model readiness check failed: {exc}"

    if models.get("easyocr"):
        try:
            easyocr_mod = importlib.import_module("easyocr")
            if not callable(getattr(easyocr_mod, "Reader", None)):
                return False, "easyocr.Reader unavailable."
        except Exception as exc:
            return False, f"EasyOCR model readiness check failed: {exc}"

    if models.get("gemini_translation"):
        try:
            genai_mod = importlib.import_module("google.genai")
            if not hasattr(genai_mod, "Client"):
                return False, "google.genai.Client unavailable."
        except Exception as exc:
            return False, f"Gemini SDK readiness check failed: {exc}"

    if models.get("tts"):
        try:
            importlib.import_module("capcut_tts_api")
            importlib.import_module("edge_tts")
        except Exception as exc:
            return False, f"TTS runtime readiness check failed: {exc}"

    for ckpt_spec in models.get("required_model_files", []):
        rel_path = ckpt_spec.get("path", "")
        ckpt_path = _resolve_in_workdir(workdir, rel_path)
        if not ckpt_path.is_file() or ckpt_path.stat().st_size == 0:
            return False, f"Model file missing or empty: {rel_path}"
        expected_sha = ckpt_spec.get("sha256")
        if expected_sha and _sha256_file(ckpt_path) != expected_sha:
            return False, f"Model checksum mismatch for {rel_path}"

    required_mods = set(manifest.get("required_v1_modules") or [])
    if (
        "subtitle_timing.py" in required_mods
        and (workdir / "subtitle_timing.py").is_file()
        and (workdir / "subtitle_layout.py").is_file()
    ):
        workdir_str = str(workdir)
        added_path = workdir_str not in sys.path
        if added_path:
            sys.path.insert(0, workdir_str)
        try:
            _clear_subtitle_layout_caches()
            subtitle_timing_mod = importlib.import_module("subtitle_timing")
            subtitle_timing_mod.resolve_cloud_subtitle_font("Arial", bold=True)
        except Exception as exc:
            return False, f"Subtitle font readiness check failed: {exc}"
        finally:
            if added_path and workdir_str in sys.path:
                try:
                    sys.path.remove(workdir_str)
                except ValueError:
                    pass

    return True, "All model runtimes verified."


def _verify_bundle(bundle_manifest_path: Path, env_manifest: Dict[str, Any], workdir: Path) -> Tuple[bool, str]:
    if not bundle_manifest_path.is_file():
        return False, f"Bundle manifest not found: {bundle_manifest_path}"
    try:
        bundle_data = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Invalid bundle manifest JSON: {exc}"

    for entry in bundle_data.get("files", []):
        name = entry.get("name", "")
        target = workdir / name
        if not target.is_file():
            return False, f"Bundled file missing in workdir: {name}"
        actual_size = target.stat().st_size
        if entry.get("size_bytes") is not None and actual_size != int(entry["size_bytes"]):
            return False, f"Bundled file size mismatch for {name}: expected {entry['size_bytes']}, got {actual_size}"
        expected_sha = entry.get("sha256")
        if expected_sha and _sha256_file(target) != expected_sha:
            return False, f"Bundled file SHA256 mismatch for {name}"

    for mod_name in env_manifest.get("required_v1_modules", []):
        mod_path = workdir / mod_name
        if not mod_path.is_file() or mod_path.stat().st_size == 0:
            return False, f"Required V1 module missing or empty in workdir: {mod_name}"

    expected_video = bundle_data.get("expected_video")
    if isinstance(expected_video, dict) and expected_video.get("remote_filename"):
        v_name = expected_video["remote_filename"]
        v_path = workdir / v_name
        if not v_path.is_file():
            return False, f"Uploaded video missing in workdir: {v_name}"
        v_size = v_path.stat().st_size
        if v_size <= 0:
            return False, f"Uploaded video is empty (0 bytes): {v_name}"
        if expected_video.get("size_bytes") is not None and v_size != int(expected_video["size_bytes"]):
            return (
                False,
                f"Uploaded video size mismatch for {v_name}: expected {expected_video['size_bytes']}, got {v_size}",
            )
        if expected_video.get("sha256"):
            actual_v_sha = _sha256_file(v_path)
            if actual_v_sha != expected_video["sha256"]:
                return False, f"Uploaded video SHA256 mismatch for {v_name}"

    return True, "Bundle and uploaded input verified cleanly."


SETUP_PHASES_ENV = "AUTODUB_SETUP_PHASES"


def _inherited_setup_phases() -> Dict[str, float]:
    try:
        raw = json.loads(os.environ.get(SETUP_PHASES_ENV) or "{}")
        return {str(k): round(float(v), 3) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (TypeError, ValueError):
        return {}


_SETUP_PHASES: Dict[str, float] = {}


def _record_phase(name: str, started_at: float) -> None:
    _SETUP_PHASES[name] = round(_SETUP_PHASES.get(name, 0.0) + max(0.0, time.monotonic() - started_at), 3)


def _child_env_with_phases() -> Dict[str, str]:
    env = dict(os.environ)
    env[SETUP_PHASES_ENV] = json.dumps(_SETUP_PHASES)
    return env


def _emit_and_exit(
    *,
    workdir: Path,
    manifest_out: Path,
    started_at: float,
    ok: bool,
    exit_code: int,
    error_code: Optional[str],
    message: str,
    cache_hit: bool = False,
    pip_invoked: bool = False,
    gpu_info: Optional[Dict[str, Any]] = None,
    disk_info: Optional[Dict[str, Any]] = None,
    versions_info: Optional[Dict[str, Any]] = None,
    bundle_verified: bool = False,
    manifest_sha256: Optional[str] = None,
    state_file: Optional[Path] = None,
    setup_fingerprint: Optional[str] = None,
) -> int:
    payload: Dict[str, Any] = {
        "ok": ok,
        "exit_code": exit_code,
        "error_code": error_code,
        "message": message,
        "cache_hit": cache_hit,
        "pip_invoked": pip_invoked,
        "gpu": gpu_info or {},
        "disk": disk_info or {},
        "versions": versions_info or {},
        "bundle_verified": bundle_verified,
        "manifest_sha256": manifest_sha256,
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "phase_seconds": dict(_SETUP_PHASES),
    }
    try:
        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        manifest_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    if ok and state_file is not None and setup_fingerprint:
        state_data = {
            "ok": True,
            "setup_fingerprint": setup_fingerprint,
            "manifest_sha256": manifest_sha256,
            "python": sys.version.split()[0],
            "updated_at_epoch": time.time(),
        }
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
        except OSError:
            pass

    print(f"{RESULT_PREFIX}{json.dumps(payload, ensure_ascii=False)}", flush=True)
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    started = time.monotonic()
    _SETUP_PHASES.clear()
    _SETUP_PHASES.update(_inherited_setup_phases())
    parser = argparse.ArgumentParser(description="Cloud GPU Environment Setup & Preflight")
    parser.add_argument("--workdir", default=".", help="Working directory (default: current dir)")
    parser.add_argument("--manifest", default="cloud_env_manifest.json", help="Path to cloud_env_manifest.json")
    parser.add_argument("--expected-gpu", default="T4", help="Expected GPU family (T4, L4, A100, CPU, etc.)")
    parser.add_argument("--allow-any-gpu", action="store_true", help="Skip strict GPU hardware check")
    parser.add_argument("--min-disk-mb", type=int, default=0, help="Minimum required free disk space in MB")
    parser.add_argument("--verify-bundle", default="", help="Optional path to bundle_manifest.json")
    parser.add_argument("--manifest-out", default="env_manifest_result.json", help="Output result JSON path")
    parser.add_argument("--state-file", default=".autodub_env_state.json", help="Idempotency state file path")
    parser.add_argument("--pip-timeout", type=int, default=120, help="Pip network timeout in seconds")
    parser.add_argument("--verify-installed", action="store_true", help="Verify in a fresh process after installation; never reinstall")
    parser.add_argument(
        "--allow-local-test",
        action="store_true",
        help="Explicitly allow running on Windows / local test harness without mutating venv packages",
    )
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    manifest_out = _resolve_in_workdir(workdir, args.manifest_out)
    state_file = _resolve_in_workdir(workdir, args.state_file)
    manifest_path = _resolve_in_workdir(workdir, args.manifest)

    if not args.allow_local_test:
        blocked, block_reason = _is_production_local_env()
        if blocked:
            return _emit_and_exit(
                workdir=workdir,
                manifest_out=manifest_out,
                started_at=started,
                ok=False,
                exit_code=EXIT_ENV_SETUP_FAILED,
                error_code="ENV_SETUP_FAILED",
                message=block_reason,
            )

    if not manifest_path.is_file():
        return _emit_and_exit(
            workdir=workdir,
            manifest_out=manifest_out,
            started_at=started,
            ok=False,
            exit_code=EXIT_ENV_SETUP_FAILED,
            error_code="ENV_SETUP_FAILED",
            message=f"Environment manifest not found: {manifest_path}",
        )

    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _emit_and_exit(
            workdir=workdir,
            manifest_out=manifest_out,
            started_at=started,
            ok=False,
            exit_code=EXIT_ENV_SETUP_FAILED,
            error_code="ENV_SETUP_FAILED",
            message=f"Invalid cloud_env_manifest.json: {exc}",
        )

    req_path = _resolve_in_workdir(workdir, manifest.get("requirements_file", "requirements_cloud.txt"))
    req_sha = _sha256_file(req_path) if req_path.is_file() else ""
    setup_fingerprint = hashlib.sha256(
        f"{manifest_sha}:{req_sha}:{sys.version.split()[0]}".encode("utf-8")
    ).hexdigest()

    # 1. Disk Space Preflight
    min_disk_mb = int(args.min_disk_mb or manifest.get("min_free_disk_mb", 4096))
    usage = shutil.disk_usage(workdir)
    free_mb = int(usage.free // (1024 * 1024))
    disk_info = {"path": str(workdir), "free_mb": free_mb, "min_disk_mb": min_disk_mb}
    if free_mb < min_disk_mb:
        return _emit_and_exit(
            workdir=workdir,
            manifest_out=manifest_out,
            started_at=started,
            ok=False,
            exit_code=EXIT_DISK_INSUFFICIENT,
            error_code="ENV_DISK_INSUFFICIENT",
            message=f"Insufficient free disk space: {free_mb} MB available < {min_disk_mb} MB required.",
            disk_info=disk_info,
            manifest_sha256=manifest_sha,
        )

    # 2. GPU Driver / Hardware Preflight (fail early before pip install if GPU wrong/missing)
    gpu_info = _detect_gpu()
    gpu_info["expected_gpu"] = args.expected_gpu
    family_patterns = manifest.get("gpu_family_patterns") or {}
    gpu_ok, gpu_msg = _gpu_matches_expected(
        args.expected_gpu,
        gpu_info,
        family_patterns,
        allow_any_gpu=args.allow_any_gpu,
        require_torch_cuda=False,
    )
    if not gpu_ok:
        return _emit_and_exit(
            workdir=workdir,
            manifest_out=manifest_out,
            started_at=started,
            ok=False,
            exit_code=EXIT_GPU_MISMATCH,
            error_code="ENV_GPU_MISMATCH",
            message=gpu_msg,
            gpu_info=gpu_info,
            disk_info=disk_info,
            manifest_sha256=manifest_sha,
        )

    runtime_spec = manifest.get("isolated_runtime")
    runtime_dir = (workdir / ".autodub_venv").absolute()
    if runtime_spec and not args.allow_local_test and Path(sys.prefix).absolute() != runtime_dir:
        try:
            from bootstrap_runtime import ensure_runtime
            phase_t0 = time.monotonic()
            runtime_python = ensure_runtime(workdir, runtime_spec)
            _record_phase("bootstrap_runtime", phase_t0)
            child = subprocess.run([str(runtime_python), str(Path(__file__).absolute()),
                                    *(argv if argv is not None else sys.argv[1:])],
                                   cwd=workdir, timeout=1200, shell=False, env=_child_env_with_phases())
            return child.returncode
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return _emit_and_exit(workdir=workdir, manifest_out=manifest_out, started_at=started,
                                  ok=False, exit_code=EXIT_ENV_SETUP_FAILED,
                                  error_code="ENV_SETUP_FAILED", message=str(exc))

    # 3. Idempotent Package/Binary Check -> Pip Install Only If Missing/Changed
    cache_hit = False
    pip_invoked = bool(args.verify_installed)
    state_matches = False
    if state_file.is_file():
        try:
            prev_state = json.loads(state_file.read_text(encoding="utf-8"))
            state_matches = bool(
                prev_state.get("ok") is True and prev_state.get("setup_fingerprint") == setup_fingerprint
            )
        except (OSError, json.JSONDecodeError):
            state_matches = False

    phase_t0 = time.monotonic()
    pkg_ok, pkg_rc, pkg_code, pkg_msg, versions_info = _verify_packages_and_binaries(manifest, workdir)
    _record_phase("verify_packages", phase_t0)
    if pkg_ok:
        cache_hit = not args.verify_installed
    else:
        # If local wheel SHA256 itself is corrupt or missing, do not attempt pip install
        if args.verify_installed or "checksum mismatch" in pkg_msg.lower() or "wheel file missing" in pkg_msg.lower():
            return _emit_and_exit(
                workdir=workdir,
                manifest_out=manifest_out,
                started_at=started,
                ok=False,
                exit_code=EXIT_PACKAGE_MISSING,
                error_code="ENV_PACKAGE_MISSING",
                message=pkg_msg,
                cache_hit=False,
                pip_invoked=False,
                gpu_info=gpu_info,
                disk_info=disk_info,
                versions_info=versions_info,
                manifest_sha256=manifest_sha,
            )

        blocked_host, block_msg = _is_production_local_env()
        if blocked_host and subprocess.run is _ORIGINAL_SUBPROCESS_RUN:
            return _emit_and_exit(
                workdir=workdir,
                manifest_out=manifest_out,
                started_at=started,
                ok=False,
                exit_code=pkg_rc,
                error_code=pkg_code,
                message=f"Refusing unmocked pip install on local host; missing/mismatched dependency: {pkg_msg}",
                cache_hit=False,
                pip_invoked=False,
                gpu_info=gpu_info,
                disk_info=disk_info,
                versions_info=versions_info,
                manifest_sha256=manifest_sha,
            )

        pip_invoked = True
        phase_t0 = time.monotonic()
        inst_ok, inst_rc, inst_code, inst_msg = _install_dependencies(
            manifest,
            workdir,
            pip_timeout=int(args.pip_timeout),
        )
        _record_phase("install_dependencies", phase_t0)
        if not inst_ok:
            return _emit_and_exit(
                workdir=workdir,
                manifest_out=manifest_out,
                started_at=started,
                ok=False,
                exit_code=inst_rc,
                error_code=inst_code,
                message=inst_msg,
                cache_hit=False,
                pip_invoked=True,
                gpu_info=gpu_info,
                disk_info=disk_info,
                versions_info=versions_info,
                manifest_sha256=manifest_sha,
            )

        # Torch's initial GPU probe may have imported NumPy before pip replaced it.
        # Native extensions must be validated in a fresh interpreter, not that cache.
        if not args.allow_local_test:
            child_args = list(argv if argv is not None else sys.argv[1:])
            child = subprocess.run([sys.executable, str(Path(__file__).absolute()),
                                    *child_args, "--verify-installed"], cwd=workdir,
                                   timeout=300, shell=False, env=_child_env_with_phases())
            return child.returncode

        pkg_ok, pkg_rc, pkg_code, pkg_msg, versions_info = _verify_packages_and_binaries(manifest, workdir)
        if not pkg_ok:
            return _emit_and_exit(
                workdir=workdir,
                manifest_out=manifest_out,
                started_at=started,
                ok=False,
                exit_code=pkg_rc,
                error_code=pkg_code,
                message=pkg_msg,
                cache_hit=False,
                pip_invoked=True,
                gpu_info=gpu_info,
                disk_info=disk_info,
                versions_info=versions_info,
                manifest_sha256=manifest_sha,
            )

    # Re-check torch CUDA availability after ensuring packages exist
    gpu_info = _detect_gpu()
    gpu_info["expected_gpu"] = args.expected_gpu
    cuda_ok, cuda_msg = _gpu_matches_expected(
        args.expected_gpu,
        gpu_info,
        family_patterns,
        allow_any_gpu=args.allow_any_gpu,
        require_torch_cuda=True,
    )
    if not cuda_ok:
        return _emit_and_exit(
            workdir=workdir,
            manifest_out=manifest_out,
            started_at=started,
            ok=False,
            exit_code=EXIT_GPU_MISMATCH,
            error_code="ENV_GPU_MISMATCH",
            message=cuda_msg,
            cache_hit=cache_hit and state_matches,
            pip_invoked=pip_invoked,
            gpu_info=gpu_info,
            disk_info=disk_info,
            versions_info=versions_info,
            manifest_sha256=manifest_sha,
        )

    # 4. Stage bundled host fonts, ensure Linux fonts & Model Runtime Readiness Check
    _stage_bundled_subtitle_fonts(workdir, allow_local_test=bool(args.allow_local_test))
    _ensure_linux_subtitle_fonts(allow_local_test=bool(args.allow_local_test), workdir=workdir)
    phase_t0 = time.monotonic()
    model_ok, model_msg = _verify_models_readiness(manifest, workdir)
    _record_phase("verify_models", phase_t0)
    if not model_ok:
        return _emit_and_exit(
            workdir=workdir,
            manifest_out=manifest_out,
            started_at=started,
            ok=False,
            exit_code=EXIT_MODEL_LOAD_FAILED,
            error_code="ENV_MODEL_LOAD_FAILED",
            message=model_msg,
            cache_hit=cache_hit and state_matches,
            pip_invoked=pip_invoked,
            gpu_info=gpu_info,
            disk_info=disk_info,
            versions_info=versions_info,
            manifest_sha256=manifest_sha,
        )

    # 5. Bundle & Uploaded Video Verification
    bundle_verified = False
    if args.verify_bundle:
        bundle_path = _resolve_in_workdir(workdir, args.verify_bundle)
        b_ok, b_msg = _verify_bundle(bundle_path, manifest, workdir)
        if not b_ok:
            return _emit_and_exit(
                workdir=workdir,
                manifest_out=manifest_out,
                started_at=started,
                ok=False,
                exit_code=EXIT_UPLOAD_VERIFICATION_FAILED,
                error_code="UPLOAD_VERIFICATION_FAILED",
                message=b_msg,
                cache_hit=cache_hit and state_matches,
                pip_invoked=pip_invoked,
                gpu_info=gpu_info,
                disk_info=disk_info,
                versions_info=versions_info,
                bundle_verified=False,
                manifest_sha256=manifest_sha,
            )
        bundle_verified = True

    return _emit_and_exit(
        workdir=workdir,
        manifest_out=manifest_out,
        started_at=started,
        ok=True,
        exit_code=EXIT_OK,
        error_code=None,
        message="Cloud GPU environment verified and ready.",
        cache_hit=cache_hit and state_matches,
        pip_invoked=pip_invoked,
        gpu_info=gpu_info,
        disk_info=disk_info,
        versions_info=versions_info,
        bundle_verified=bundle_verified,
        manifest_sha256=manifest_sha,
        state_file=state_file,
        setup_fingerprint=setup_fingerprint,
    )


if __name__ == "__main__":
    raise SystemExit(main())
