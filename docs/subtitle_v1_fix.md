# Tool V1: nhận diện và che phụ đề

Áp dụng trong backend/ocr_utils.py, backend/ocr_subtitle_locator.py,
backend/ass_utils.py và hai API xuất video trong backend/main.py.

- OCR lấy ba frame mỗi câu (một frame nếu câu ngắn hơn 250 ms), tối đa
  12 ảnh mỗi lần gọi model. Video dài sẽ tốn thời gian OCR hơn bản cũ.
- Mỗi kết quả giữ ID câu và timestamp. Chỉ khối khớp transcript mới vào
  tracking. Không dùng chữ thay đổi theo ảnh hoặc khối rộng nhất để tự suy ra subtitle.
- Chữ gần giống, cùng vùng, lặp qua ít nhất ba câu nhưng chỉ khớp một
  phần nhỏ lời thoại bị loại như nhãn sản phẩm.
- Hai dòng/mảnh cùng dòng chỉ được gộp trong cùng frame và khi việc gộp
  cải thiện độ khớp transcript.
- Vị trí tracking đổi tại trung điểm hai lần lấy mẫu; ASS cắt theo thời gian
  câu dịch (kể cả câu bị tách). Không kéo một block hết hạn vào câu sau.
- Hộp che dùng X/Y và kích thước OCR cộng padding, mở rộng cho chữ Việt.
  Vùng sát mép không bị ép vào lề. Canvas giữ tỷ lệ video, gồm video vuông.
- Không tìm được khối đáng tin cậy: chỉ hiện chữ Việt, không vẽ hộp che đoán.
  Đây là lựa chọn thận trọng để hạn chế che sản phẩm, không phải cam kết OCR
  nhận diện được mọi font hoặc mọi chuyển động giữa các frame.
- Giữ nguyên runtime V1, font/tách câu của dự án, xử lý batch/Telegram.
  Hai API desktop hiện chạy OCR trước dịch và render bằng ASS thay cho SRT.

Kiểm tra:
    .\backend\venv\Scripts\python.exe -m unittest discover -s tests

Các test đặc thù nằm ở tests/test_ocr_and_ass_upgrades.py và
tests/test_subtitle_tracking_regression.py. Có kiểm tra đường OCR giả lập
đến ASS, bao bì sát cùng dải Y, font/video vuông, hộp che sát mép và API.

Đã thử EasyOCR CPU thực với hai câu và render FFmpeg trên video có sẵn:
1788548817_feb50704_今日苏讯179普四少年感男体开箱测评.
Một câu được chọn đúng vùng chữ lệch trái; một câu thiếu độ tin cậy và
không tạo hộp che. Không chạy dịch/TTS hay xuất lại toàn bộ video.

