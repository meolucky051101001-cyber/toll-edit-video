# BÁO CÁO KỸ THUẬT: DỌN DẸP AN TOÀN VÀ TỐI ƯU MÃ NGUỒN TOOL V1 (C0 – C9)

- **Ngày thực hiện:** 01/11/2026 – 02/11/2026
- **Kế hoạch nguồn:** `CLEANUP_PLAN.md` & `REVIEW.md`
- **Mốc neo trước cleanup (Baseline Tag):** `v1-pre-cleanup-20261101` (`d8cff9241336ff7ce7b7b1efb0ec02f4008efa6f`)
- **Nhánh triển khai:** `codex/toolv1-cleanup` tại `C:\tool_v1_updates`
- **Runtime kiểm chuẩn:** Python 3.12.14 (`backend/venv`), Python 3.10.11 (`backend/venv_py310`), Node.js 20+

---

## 1. MỤC TIÊU VÀ KẾT QUẢ ĐẠT ĐƯỢC

1. **Thu hẹp phạm vi dừng dịch vụ (`F01` - C1):** Các lệnh dừng (`TatHoanToan.bat`, `stop_bot.bat`, `stop_telebot_downloader.bat`) chuyển sang sử dụng bộ điều phối chung `backend/v1_shutdown.py`. Chỉ tác động đúng tiến trình thuộc thư mục dự án, kiểm tra khớp file thực thi, script và thời gian khởi tạo; loại bỏ việc tin PID cũ và loại bỏ việc kill theo tên tiến trình trên toàn hệ thống. Hỗ trợ cờ `--preview` và chặn dừng khi hệ thống đang bận (`busy`).
2. **Vô hiệu hóa công cụ lịch sử có side effect (`F02`, `F03` - C2):** 9 script bảo trì/rollback cũ được thay thế tại chỗ bằng stub an toàn trả mã lỗi `1`, ngăn chặn việc vô tình ghi đè queue, config hoặc mã nguồn khi chạy nhầm hoặc import.
3. **Loại bỏ mã chết và trạng thái thừa (`F08` - C3):** Dọn dẹp nhánh EasyOCR không thể chạm tới trong `backend/ocr_utils.py` và các biến lỗi thời trong `backend/ai/translation.py`; giữ nguyên 3 module HOLD (`v1_free_gpt.py`, `v1_telegram_errors.py`, `qc_service.py`) để bảo toàn hợp đồng hệ thống.
4. **Hợp nhất Whiteboard Animator (`F04` - C5):** Chuẩn hóa `backend/whiteboard_animator/` làm bản thực thi duy nhất (bảo toàn cải tiến NVENC, extra frames, timing fit); chuyển 11 file con lồng nhau thành shim re-export mỏng để giữ tương thích ngược.
5. **Thu thập test vào collection chuẩn (`F09` - C6):** Di chuyển 8 ca kiểm thử hồi quy của `RecoveryService` vào `tests/test_recovery_service.py` với cơ chế cô lập thư mục tạm và router.
6. **Thống nhất đóng gói phát hành (`F07` - C7):** Đồng bộ tuyệt đối chính sách chọn file phát hành (561 file sau khi thêm tài liệu báo cáo) giữa `package_v1_release.py` và `scripts/generate_release_manifest.py`, hỗ trợ `--dry-run` và ghi tạm an toàn.
7. **CI đa phiên bản (`F10` - C8):** Cập nhật `.github/workflows/python-quality.yml` chạy ma trận song song Python 3.12 (chính) + Python 3.10 (dự phòng) với 174 bài kiểm thử tất định và 11 bài kiểm thử Node.
8. **Dọn dẹp mã nguồn debug lịch sử (`F05`, `F06` - C4):** Thực hiện `git rm` 69 file (65 file debug lịch sử + 4 file test thủ công phụ thuộc môi trường máy chủ) sau khi người dùng phê duyệt manifest, loại bỏ 6.321 dòng mã không sử dụng.

---

## 2. CHUỖI COMMIT ĐÃ THỰC HIỆN

| Gói | Commit | Loại | Thông điệp commit | Tóm tắt thay đổi |
| --- | --- | --- | --- | --- |
| **C0** | `d8cff92` | `tag` | `v1-pre-cleanup-20261101` | Khóa baseline test, golden hashes, AST audits và danh sách 88 file ứng viên. |
| **C1** | `32f3fe3` | `fix` | `fix(v1): scope service shutdown to verified project processes` | Thêm `backend/v1_shutdown.py`, cập nhật `process_killer.py`, các file `.bat`, thêm 10 tests trong `tests/test_v1_shutdown_scope.py`. |
| **C2** | `c08357b` | `fix` | `fix(v1): retire unsafe one-off maintenance entrypoints` | Thay thế tại chỗ 9 script cũ bằng stub an toàn, thêm 11 tests trong `tests/test_v1_retired_entrypoints.py`. |
| **C3** | `0c468a4` | `refactor` | `refactor(v1): remove unreachable OCR and obsolete translation state` | Xóa mã chết OCR & translation, thêm 4 tests trong `tests/test_v1_c3_cleanup_contracts.py`. |
| **C6** | `236cb18` | `test` | `test(v1): collect isolated recovery regression cases` | Chuyển 8 tests sang `tests/test_recovery_service.py`, shim hóa `backend/test_recovery_service.py`. |
| **C5** | `a6b62e0` | `refactor` | `refactor(v1): consolidate whiteboard implementations` | Hợp nhất Whiteboard Animator, chuyển 11 file con thành re-export shim, thêm 5 tests trong `tests/test_whiteboard_consolidation.py`. |
| **C7** | `e82c7a4` | `fix` | `fix(v1): align release packaging with supported runtime` | Đồng bộ quy tắc chọn 560 file phát hành, hỗ trợ `--dry-run`, thêm 1 test trong `tests/test_release_regressions.py`. |
| **C8** | `362597f` | `ci` | `ci(v1): verify Python 3.12 and document supported tooling` | Cập nhật GitHub Actions CI matrix (3.12 + 3.10 + Node) và tài liệu `README.md`, `README_FIRST.md`. |
| **C4** | `de29f17` | `refactor` | `refactor(v1): retire reviewed historical debug sources` | `git rm` 69 file debug/test lịch sử sau khi được người dùng xác nhận theo quy tắc an toàn. |

---

## 3. ĐỐI CHIẾU KẾT QUẢ KIỂM THỬ TRƯỚC VÀ SAU CLEANUP

| Chỉ số kiểm định | Baseline C0 (`d8cff92`) | Sau Cleanup C9 (`de29f17`) | Ghi chú |
| --- | --- | --- | --- |
| **Pytest Full Suite (Python 3.12.14)** | `1178 passed, 0 failed, 79 skipped` | **`1217 passed, 0 failed, 79 skipped`** | Tăng thêm **+39 bài test mới**, bảo toàn 100% hợp đồng |
| **Pytest Full Suite (Python 3.10.11)** | `1178 passed, 0 failed, 79 skipped` | **`1217 passed, 0 failed, 79 skipped`** | Tương thích tuyệt đối trên môi trường dự phòng rollback |
| **Node.js Tests (`node --test`)** | `11 passed, 0 failed` | **`11 passed, 0 failed`** | Kiểm thử giao diện và subtitle rendering giữ nguyên |
| **CI Deterministic Suite** | 12 files (Python 3.10) | **17 files / 174 passed** (Python 3.12 + 3.10) | Bổ sung các bài test an toàn cho shutdown, retired scripts, C3, whiteboard |
| **Phase 7 AST Audits** | 100% PASS | **100% PASS** | `audit_ocr.py`, `audit_batch.py`, `audit_orchestrator.py` |
| **Visual Golden Cases (ASS)** | 72/72 khớp (100%) | **72/72 khớp (100%)** | `scratch/phase7/compare_ass.py` không có sai lệch |
| **PowerShell Syntax Check** | 6/6 PASS | **6/6 PASS** | Kiểm tra toàn bộ script `.ps1` qua AST parser |
| **Release Manifest Parity** | Lệch giữa ZIP và manifest | **561 files khớp tuyệt đối** | Dry-run PASS, không chứa secret/model |
| **Runtime Health Check (chỉ đọc)** | 25/25 PASS | **25/25 PASS** | `scripts/verify_runtime_health.py --timeout 30` |

---

## 4. CHI TIẾT CÁC NHÓM FILE ĐƯỢC XỬ LÝ

### 4.1. Nhóm 69 file đã xóa vật lý (`git rm` ở commit `de29f17`)
- **Bản tham chiếu cũ:** `backend/ai/_kinetic_v3ref.py` (2.657 dòng).
- **59 file debug/crawler lịch sử:** `backend/scripts_and_tests/*.py` (các script test mạng/douyin/xhs độc lập có hardcode đường dẫn cũ).
- **5 file script one-off tại `backend/`:** `check_today_batch.py`, `check_voice_catalog.py`, `find_all_links.py`, `test_verify_codex.py`, `update_dashboards_phase1.py`.
- **4 script test thủ công:** `backend/test_links.py`, `backend/test_segment_editor.py`, `backend/test_voice_overlap_fix.py`, `backend/test_voice_preview.py`.

### 4.2. Nhóm 9 script lịch sử được thay thế tại chỗ bằng stub an toàn (`commit c08357b`)
- `backend/enqueue_missing_batch.py`
- `backend/sync_capcut_queue.py`
- `RESTORE_SAFE_BASELINE.bat`
- `ROLLBACK_TRANSLATION_FIX.bat`
- `RESTORE_VOICE_LOCK_BACKUP.bat`
- `rollback_to_safe_state.ps1`
- `fixes_for_v1/apply_fix_to_v1.bat`
- `fixes_for_v1/rollback_tool_v1.bat`
- `scripts/setup_telegram_bot_v2.ps1`

### 4.3. Nhóm 12 file được chuyển thành re-export shim mỏng
- `backend/test_recovery_service.py` → trỏ về `tests/test_recovery_service.py`.
- 11 file trong `backend/whiteboard_animator/whiteboard_animator/*.py` → trỏ về `backend/whiteboard_animator/<module>.py`.

### 4.4. Nhóm 3 module HOLD (giữ nguyên theo hợp đồng hệ thống)
- `backend/ai/v1_free_gpt.py`
- `backend/v1_telegram_errors.py`
- `backend/qc_service.py`

---

## 5. HƯỚNG DẪN HOÀN TÁC (ROLLBACK)

Nếu phát sinh vấn đề ở bất kỳ thành phần nào, có thể hoàn tác an toàn:

- **Trước khi hoàn tác:** kiểm tra `git status`, bảo toàn thay đổi chưa commit
  và ghi checkpoint. Tạo commit revert cho đúng gói cần sửa, kiểm thử lại trước
  khi đồng bộ production. Không dùng reset/clean hoặc force-push để rollback.
- Không chạy rollback trực tiếp trên dịch vụ đang xử lý job. Việc đồng bộ và
  restart cần bước vận hành riêng khi idle và được cho phép.
- **Hoàn tác từng commit riêng lẻ bằng `git revert`:**
  - `de29f17` — Phục hồi lại 69 file debug/test lịch sử.
  - `362597f` — Phục hồi cấu hình CI và tài liệu cũ.
  - `e82c7a4` — Phục hồi cấu hình đóng gói phát hành cũ.
  - `a6b62e0` — Phục hồi cây thư mục lồng nhau của Whiteboard Animator.
  - `236cb18` — Phục hồi vị trí test cũ của RecoveryService.
  - `0c468a4` — Phục hồi mã EasyOCR và biến trong module dịch thuật.
  - Với `c08357b` và `32f3fe3`, ưu tiên fix-forward; không bật lại script ghi đè
    dữ liệu hoặc cách kill rộng chỉ để quay về code cũ.
