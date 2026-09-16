# Review vòng 12 — xác minh sửa báo cáo vòng 11
Ngày: 2026-09-07
Codebase: C:/Users/admin/Projects/ai-video-research-tool

## Kết luận
Lỗi fallback trả lại URL note khác ID của vòng 11 đã được sửa cho URL note đầy đủ. Antigravity bổ sung canonical URL vào merge, lấy ID từ canonical khi thiếu platform_video_id, kiểm tra share URL cả insert và duplicate. Có integration tests mới bảo vệ status/JobVideo. ROADMAP đã ghi rõ live crawler smoke pending.

Chưa hoàn thiện hoàn toàn: hai trường hợp về tính nhất quán danh tính còn thiếu. Đây là probe dữ liệu tổng hợp; chưa khẳng định đã xảy ra trên dữ liệu người dùng hoặc crawler live.

## F1 — P2: short link chưa resolve được coi là đã xác minh
backend/services/search_service.py:86,103
_evaluate trả (True, has_token, True) cho mọi URL xhslink.com hợp lệ về host. Không xác minh đích note ID. Nếu incoming short link có query xsec_token, nó được ưu tiên trước existing token URL đã xác minh.
Probe: record A, existing canonical A?xsec_token=VALID, incoming https://xhslink.com/unknown?xsec_token=UNVERIFIED → merge trả incoming short.
Hệ quả: có thể thay link đúng bằng short link hỏng hoặc trỏ bài khác; có token query không chứng minh danh tính hay tính hợp lệ của token.

Sửa:
- Phân biệt verified note URL và unresolved short URL; không cho short chưa resolve ghi đè link đã xác minh.
- Resolve short link ở adapter trước merge, giới hạn redirect, kiểm tra host/protocol và đối chiếu ID cuối cùng.
- Nếu chưa resolve được, giữ canonical/token đã xác minh. Có thể giữ raw short link trong trường provenance riêng, không coi là preferred share URL.
- Test short→A hợp lệ, short→B từ chối, timeout, short có token giả không thay token A.
Không cần loại bỏ hỗ trợ short link; cần thêm bước xác minh đích.

## F2 — P2: ID và canonical mâu thuẫn bị biến thành hai liên kết khác video
backend/services/search_service.py:53-73; backend/schemas/contracts.py VideoResult
Probe VideoResult(platform=xiaohongshu, platform_video_id=B, url=canonical A) vẫn hợp lệ.
merge_share_url(platform, B, canonical A, None, canonical A) trả canonical B, vì ID được ưu tiên và hàm tự dựng URL từ ID.
persist_results có thể giữ url/canonical A nhưng lưu ID và share URL B. Điều này trái mục tiêu card và link cùng một video.

Sửa:
- Nếu ID parse từ canonical khác platform_video_id, reject/quarantine candidate trước find_existing và persistence; không âm thầm dựng URL từ ID để chữa mâu thuẫn.
- Nếu ID thiếu thì mới suy ra từ canonical.
- Áp dụng cho insert và duplicate; không refresh metadata của record khác do OR dedup.
- Test mismatch B/A bị từ chối không tạo row/JobVideo; trường hợp thiếu ID vẫn hoạt động.
- Với dữ liệu cũ, báo cần sửa thay vì đổi danh tính bản ghi đã saved/favorite một cách tự động.

## Kiểm thử thực hiện
- Pytest: 106 passed, 2 dependency deprecation warnings, 46.64s.
- Ruff: passed.
- Mypy: passed, 36 source files.
- Probe schema/merge: xác nhận F1/F2.
- Không chạy lại frontend build/E2E vì phạm vi thay đổi đang review là backend merge; số E2E trong ROADMAP là báo cáo của Antigravity, không phải lần kiểm chứng mới.
- Không chạy crawler/Gemini, không ghi DB người dùng, không sửa source.
- Codebase không có git repo nên không có diff commit để chứng minh toàn bộ thay đổi.

## Plan gửi Antigravity
1. Sửa F1 và F2, gom policy identity thành helper để schema/adapter/persistence sử dụng nhất quán.
2. Bổ sung regression tests theo các case nêu trên, gồm API readback và giữ trạng thái thư viện.
3. Chạy backend gates; nếu frontend đổi thì build trước E2E, chạy tuần tự.
4. Live smoke 5 video mỗi nền tảng, AI off, mở đúng ID/title, tìm lại giữ token, save/reload và cancel/resume. Ghi partial/blocked nếu nền tảng chặn.
5. Sau khi đạt, tiếp tục kế hoạch download riêng đã bàn giao. Phase 5 đa nền tảng vẫn chưa triển khai; không đánh dấu hoàn tất chỉ vì merge tests xanh.

Tiêu chí đóng vòng: không còn merge tạo URL khác danh tính; short URL không được nâng thành verified trước resolve; có biên bản live phân biệt rõ với fixture.

