# Phụ đề tiếng Việt — pipeline 2.3.5

- Chữ xuống dòng theo chiều rộng đo từ font đang chọn, thay cho số ký tự ước
  lượng. Dòng đầu được điền đầy trước khi dùng dòng thứ hai, không cân bằng hai
  dòng khi dòng đầu vẫn còn chỗ.
- Với hệ tọa độ ASS 720 × 1280: chữ có tối đa 608 px (~84,4% chiều ngang),
  khung nền gồm cả viền tối đa 648 px (90%). Lề khung nhỏ nhất là 5% mỗi bên.
- Khung vẫn giữ padding dọc nhỏ. Hộp OCR giúp che phụ đề gốc ngay cả khi bản
  dịch ngắn hơn, với giới hạn chiều cao để tránh khung trắng quá lớn.
- Loại bỏ `...`, `. . .`, `…` ở đầu, giữa và cuối văn bản, không nối dính từ.
  Dấu chấm kết câu được giữ; dấu chấm trong số thập phân không tách câu.
- Mỗi câu hoàn chỉnh thành một segment trước TTS khi timing solver được bật.
  Renderer cũng tách câu sau dấu kết câu, kể cả bản dịch đã rút gọn hoặc dữ
  liệu cũ. Câu rất dài chia thành các lượt tối đa hai dòng.
- Việc chia segment giữ nguyên tổng khoảng thời gian và ID nguồn OCR. Trường
  hợp render lại bằng âm thanh đã có, mốc chia trong segment được ước lượng
  theo số từ; đây không phải căn chỉnh từng từ từ âm thanh.
- Bản 2.3.5 thay cache identity để không dùng phụ đề và audio của bản trước.

Đã kiểm tra trực quan bằng video `1788540282_5ba71009_专一的人就要干专一的事`,
đoạn có “Chốt hạ bằng trai đẹp nha, chốt luôn.” và “Ôi toàn con trai”.

Số đo font dùng Pillow và chuyển đơn vị em sang chiều cao ASS theo OS/2 Win
ascent/descent, tương ứng [cách libass chọn metric](https://github.com/libass/libass/blob/master/libass/ass_font.c).
