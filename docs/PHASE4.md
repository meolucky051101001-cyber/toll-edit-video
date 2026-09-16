# Phase 4: Douyin Provider Integration

## 1. Overview
This phase successfully integrated the **Douyin (抖音)** search provider into the AI Video Research Tool. The integration follows the established architectural pattern from Phase 3 (Xiaohongshu) while respecting Douyin's specific anti-bot mechanisms.

## 2. Technical Architecture

### 2.1 Provider Implementation
- **Parser (`backend/providers/douyin/parser.py`)**: 
  - Extracts canonical video URLs (`/video/{aweme_id}`).
  - Maps DOM attributes (title, description, duration, authors) to the common `VideoResult` schema.
  - Normalizes metric texts (e.g. converting `1.5万` to `15000`).
- **Provider (`backend/providers/douyin/provider.py`)**:
  - Automatically loads feed cards up to the specified limit (max 100).
  - Directly processes feed cards that contain titles and metrics to minimize unnecessary tab navigation and mitigate anti-bot risk.
  - For items missing complete title or metadata, navigates to detail pages sequentially in controlled loops.

### 2.2 Security & Gate Mechanisms
- **CAPTCHA Recognition**: Detects Douyin's `验证码中间页` (slider CAPTCHA page) and correctly returns `verification_required` to pause automated fetching.
- **Login Detection**: Detects login prompts (`扫码登录`) and correctly returns `login_required`.
- **User Delegation**: When blocked by CAPTCHAs, the provider cleanly delegates control to the user via the established UI `UserActionRequired` mechanism.

### 2.3 Browser Profile Isolation
- Maintains a dedicated Playwright persistent context specifically for Douyin: `data/browser/douyin`.
- Includes `MO_TRINH_DUYET_DOUYIN.bat` for users to manually open the persistent context, solve CAPTCHAs, and log in securely on their native Windows interactive desktop (`WinSta0\Default`).

## 3. Testing & Verification
- **Automated Tests**: Comprehensive unit and integration tests created at `tests/test_douyin.py` and `tests/test_pipeline.py`. Automated CI/CD and E2E tests run against isolated mock fixtures and mocked network routes without triggering external network calls.
- **Live Crawler Verification**: Live searches require actual browser launch, interactive user login/CAPTCHA resolution via the Windows desktop profile, and respectful sequential crawling.
- **Verified UI & DB**: Verified UI changes mapping "douyin" as a valid provider and enabling dynamic platform switching in the Workspace component. SQLite deduplication functions cleanly with normalized `aweme_id` values and persistent share URLs.
