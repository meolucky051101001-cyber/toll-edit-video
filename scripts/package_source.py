# scripts/package_source.py
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directories strictly excluded from clean source packaging
EXCLUDED_DIR_NAMES = {
    '.venv', 'node_modules', '.next', '__pycache__', '.pytest_cache',
    '.ruff_cache', '.mypy_cache', 'test-results', 'playwright-report',
    '.git', 'browser', 'downloads', 'dist', 'scratch'
}

# File extensions excluded generally (unless explicitly allowed in fixtures)
EXCLUDED_EXTENSIONS = {
    '.pyc', '.pyo', '.pyd', '.db', '.db-wal', '.db-shm',
    '.log', '.tar', '.gz'
}

# Specific files strictly excluded
EXCLUDED_FILE_NAMES = {
    '.env', '.env.local', '.env.production',
    'processes.json', 'downloader_processes.json',
    'review_round9_evidence.json'
}

# Explicitly allowed binary fixtures
ALLOWED_FIXTURE_PATHS = {
    Path('tests/fixtures/tiny_valid.mp4')
}


def should_include_file(rel_path: Path) -> bool:
    # 1. Check if explicitly allowed fixture
    if rel_path in ALLOWED_FIXTURE_PATHS:
        return True

    # 2. Check path parts for excluded directory names or pattern
    for part in rel_path.parts[:-1]:
        if part in EXCLUDED_DIR_NAMES or part.startswith('.codex-'):
            return False

    # 3. Check specific file name
    file_name = rel_path.name
    if file_name in EXCLUDED_FILE_NAMES or file_name.endswith('.zip'):
        return False

    # 4. Check data subdirectories
    if 'data' in rel_path.parts:
        # Only allow data/.gitkeep or explicit fixtures
        if rel_path.name != '.gitkeep':
            return False

    # 5. Check logs directory
    if 'logs' in rel_path.parts:
        # Only allow logs/.gitkeep
        if rel_path.name != '.gitkeep':
            return False

    # 6. Check extension
    ext = rel_path.suffix.lower()
    if ext in EXCLUDED_EXTENSIONS:
        return False

    return True


def build_clean_package(target_zip: Path) -> list[Path]:
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    included = []

    for dirpath, dirnames, filenames in os.walk(ROOT):
        # Prune excluded directories in-place for speed
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDED_DIR_NAMES and not d.startswith('.codex-') and d != 'data/browser'
        ]

        for fname in filenames:
            full_path = Path(dirpath) / fname
            rel_path = full_path.relative_to(ROOT)
            if should_include_file(rel_path):
                included.append((full_path, rel_path))

    with zipfile.ZipFile(target_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for full, rel in included:
            zf.write(full, rel.as_posix())

    return [rel for _, rel in included]


def verify_package(target_zip: Path) -> bool:
    with zipfile.ZipFile(target_zip, 'r') as zf:
        names = set(zf.namelist())

    # Assert forbidden files NOT present
    for forbidden in ['.env', 'data/app.db', 'data/browser/auth_state.json', 'data/app.db-wal']:
        if forbidden in names:
            raise AssertionError(f'Security violation: {forbidden} found in package!')

    for name in names:
        if name.endswith('.pyc'):
            raise AssertionError(f'Hygiene violation: .pyc found: {name}')
        if name.startswith('data/browser'):
            raise AssertionError(f'Security violation: browser profile found: {name}')
        if name.startswith('logs/') and not name.endswith('.gitkeep'):
            raise AssertionError(f'Hygiene violation: log file found: {name}')

    # Assert required files ARE present
    required = [
        'backend/app/main.py',
        'frontend/package.json',
        'tests/fixtures/tiny_valid.mp4',
        'tests/test_cookies.py',
        'IMPLEMENTATION_STATUS.md',
        'PROJECT_COMPREHENSIVE_REVIEW.md',
        'pyproject.toml',
        '.env.example'
    ]
    for req in required:
        if req not in names:
            raise AssertionError(f'Completeness violation: required file {req} missing from package!')

    print(f'Verification PASSED for {target_zip.name}: {len(names)} files, no secrets, fixtures present.')
    return True


if __name__ == '__main__':
    desktop = Path(os.environ.get('USERPROFILE', '')) / 'OneDrive' / 'Desktop'
    if not desktop.exists():
        desktop = Path(os.environ.get('USERPROFILE', '')) / 'Desktop'

    targets = [ROOT / 'AI_Video_Research_Tool_Package.zip']
    if desktop.exists():
        targets.append(desktop / 'AI_Video_Research_Tool_Package.zip')

    for target in targets:
        files = build_clean_package(target)
        verify_package(target)
        size_kb = target.stat().st_size / 1024
        print(f'Wrote {target} ({size_kb:.1f} KB, {len(files)} files)')
