# HƯỚNG DẪN ÁP DỤNG BẢN SỬA LỖI DỊCH THUẬT & LỒNG TIẾNG

> **Lưu ý**: Hiện tại Tool V1 vẫn được giữ nguyên 100% nguyên trạng (`c822af9`). Gói sửa lỗi này được lưu độc lập tại thư mục `C:\tool v2\fixes_for_v1\` để bạn có thể sử dụng bất cứ lúc nào trong tương lai.

---

## 1. NGUYÊN NHÂN GÂY LỖI
1. **Lỗi mã hóa ký tự (Mojibake)**: Bản dịch tiếng Việt từ API trả về bị giải mã nhầm qua bảng mã CP1252/Latin-1 thành các ký tự rác (`Ä‘Ã£`, `má»™t`, `ÄÆ°á»£c`...).
2. **Lồng tiếng đọc vô nghĩa**: Do subtitle bị lỗi mã hóa, engine TTS đọc từng ký tự rác Latin một cách vô nghĩa, sau đó RVC clone lại giọng đọc rác này khiến âm thanh biến dạng kỳ quái.
3. **Kẹt model fallback (`_gemini_last_good`)**: Khi `gemini-3.7-flash` bị 503/429, code tự động rơi xuống `gemini-3.5-flash-lite`. Sau khi chạy thành công 1 lần, biến `_gemini_last_good` ghim cứng model này làm ưu tiên số 1, khiến 50% video về sau bị kẹt vĩnh viễn ở model flash-lite chất lượng thấp, dễ gây ảo giác (nhầm Sumikko Gurashi thành GoroGoro Nyansuke).
4. **Bẫy cache cũ (`translation_cache`)**: File cache lỗi được lưu trong `workspace/translation_cache`, khiến các lần edit sau tiếp tục bốc bản dịch hỏng ra dùng lại.

---

## 2. CÁC CÔNG CỤ TRONG GÓI NÀY

- `clean_corrupted_cache.py`: Script tự động quét và xóa triệt để các file cache dịch thuật bị dính Mojibake.
- `mojibake_validator.py`: Hàm kiểm duyệt bản dịch trước khi chuyển sang TTS để đảm bảo 100% không còn ký tự rác.
- `apply_fix_to_v1.bat`: File cài đặt 1-chạm (tự động backup file cũ của V1 trước khi cập nhật).
- `rollback_tool_v1.bat`: File khôi phục Tool V1 về nguyên trạng gốc ngay lập tức.

---

## 3. CÁCH SỬ DỤNG KHI CẦN
Khi nào bạn muốn áp dụng bản sửa vào Tool V1:
1. Chạy file `apply_fix_to_v1.bat` bằng cách nhấp đúp chuột.
2. Chạy `python clean_corrupted_cache.py` để làm sạch các cache hỏng cũ.
3. Nếu muốn khôi phục lại V1 ban đầu: Chạy `rollback_tool_v1.bat`.

---

## 4. VIDEO ĐÃ SỬA MẪU HOÀN CHỈNH
Video đã sửa chuẩn (phụ đề chính xác Sumikko Gurashi, không còn lỗi mã hóa, giọng đọc rõ ràng, nhạc nền chuẩn) đã được xuất bản tại:
- `D:\banve\Dubbed_1789500485_a5674f83_SumikkoGurashi_FIXED.mp4`
- `C:\tool v2\workspace\repaired_videos\Dubbed_1789500485_a5674f83_SumikkoGurashi_FIXED.mp4`
