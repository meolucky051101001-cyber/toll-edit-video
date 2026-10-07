# OCR subtitle detection v2.3.3

## Vấn đề

Bộ chọn cũ xem mọi khối có chữ Hán là ứng viên phụ đề. Khi độ tương đồng
OCR/ASR thấp, nó chọn khối rộng nhất cùng thời điểm rồi hợp nhất nhiều khối.
Chữ trên bao bì, bảng sản phẩm hoặc nội dung trong cảnh vì vậy có thể kéo vùng
che khỏi dòng phụ đề thật. Trình tạo ASS cũng tính độ rộng chữ Trung nhưng không
dùng tọa độ đó để đặt vùng che, nên phụ đề dài hoặc lệch trái vẫn có thể lộ.

## Thay đổi

- Mỗi frame OCR được gắn với đúng segment ASR đã dùng để lấy mẫu.
- Ưu tiên chuỗi chữ Trung khớp liên tiếp với nội dung ASR; không còn cộng điểm
  chỉ vì một khối có một ký tự Hán.
- Gom ứng viên theo dải Y được nhiều frame độc lập xác nhận. Khi ASR nhận sai
  ngôn ngữ, bộ chọn ưu tiên dải có nội dung thay đổi theo thời gian và hạ điểm
  chữ cố định lặp lại trên sản phẩm.
- Loại khối quá cao hoặc dạng chữ dọc; hỗ trợ ghép hai dòng chỉ khi nội dung
  ghép khớp mạnh với cùng câu thoại.
- Lấy mẫu đều tối đa 24 segment trên toàn video, thay vì để video dài tạo quá
  nhiều frame tập trung ở một đoạn.
- Khóa dải Y ổn định sau khi tìm được cụm phụ đề; không theo dõi chữ sản phẩm
  chuyển động giữa các frame.
- Vùng trắng dùng cả tọa độ X/Y, chiều rộng và chiều cao của phụ đề gốc, kèm
  biên an toàn cho viền glyph mà OCR thường không bao hết.
- Tăng implementation version lên 2.3.3 để cache OCR cũ không được dùng lại.

## Xác minh

- 5 regression test cho bao bì cạnh tranh với phụ đề, ASR sai ngôn ngữ, chữ
  dọc, phụ đề hai dòng và vùng che lệch trái.
- Video lỗi có bảng sản phẩm: dải cũ `0.513–0.754`; dải mới
  `0.671–0.761`, chọn bằng so khớp ASR. Preview mới che hết phụ đề Trung và
  giữ nguyên bảng sản phẩm.
- Video có ASR cũ nhận sai ngôn ngữ: dải cũ `0.415`; dải mới
  `0.755–0.798`, chọn bằng đồng thuận không gian.
- Video đối chứng đang đúng: dải cũ `0.741`; dải mới `0.732–0.798`, vẫn bám
  đúng dòng phụ đề.

Các lượt đánh giá trên video đều chạy PP-OCRv6 cục bộ và không gửi dữ liệu tới
dịch vụ ngoài.
