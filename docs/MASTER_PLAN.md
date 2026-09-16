# MASTER PLAN — AI VIDEO RESEARCH TOOL

Tôi muốn bạn xây dựng cho tôi một công cụ chạy LOCAL trên Windows có tên tạm thời:

**AI Video Research Tool**

Công cụ có nhiệm vụ giúp tôi tìm kiếm, thu thập, phân loại và lưu các video liên quan đến một chủ đề trên:

- Douyin
- Xiaohongshu / RED

Mục tiêu chính là giảm thời gian tôi phải tìm video thủ công.

Tôi không phải lập trình viên chuyên nghiệp, vì vậy project phải:

- dễ chạy;
- dễ debug;
- cấu trúc code rõ ràng;
- có README hướng dẫn;
- có script khởi động;
- không yêu cầu tôi phải nhớ nhiều command;
- mọi lỗi quan trọng phải hiển thị dễ hiểu trên UI;
- ưu tiên chạy ổn định trước khi tối ưu.

---

# 1. MỤC TIÊU SẢN PHẨM

Workflow mong muốn:

```text
Người dùng nhập chủ đề bằng tiếng Việt
        ↓
AI hiểu chủ đề
        ↓
AI dịch + mở rộng thành keyword tiếng Trung
        ↓
AI tạo hashtag liên quan
        ↓
Search trên Douyin + Xiaohongshu
        ↓
Thu thập các video tìm được
        ↓
Chuẩn hóa metadata
        ↓
Loại video trùng
        ↓
AI đánh giá mức độ liên quan
        ↓
Xếp hạng
        ↓
Hiển thị dạng thư viện video
        ↓
Người dùng Save / Skip / Copy Link
```

Ví dụ:

Người dùng nhập:

```text
dụng cụ bóc sticker cute
```

AI có thể tạo:

```text
贴纸工具
贴纸镊子
撕膜镊子
撕膜工具
手帐工具
可爱文具
手帐好物
文具分享
```

Hashtag:

```text
#手帐
#手帐工具
#文具分享
#好物分享
#开箱
#可爱文具
```

Sau đó dùng các query này để tìm video.

---

# 2. NGUYÊN TẮC KIẾN TRÚC

Không viết logic Douyin/Xiaohongshu trực tiếp vào Search Service.

Phải sử dụng kiến trúc Provider.

Ví dụ:

```text
SearchService
      │
      ├── DouyinProvider
      │
      └── XiaohongshuProvider
```

Mỗi provider implement chung một interface.

Ví dụ concept:

```python
class SearchProvider:
    async def search(
        query: str,
        limit: int,
        filters: SearchFilters
    ) -> list[VideoResult]:
        ...
```

Mục đích:

Sau này có thể thêm:

```text
TikTokProvider
BilibiliProvider
YouTubeProvider
KuaishouProvider
PinterestProvider
```

mà không sửa core search engine.

---

# 3. API VÀ BROWSER AUTOMATION

Ưu tiên theo thứ tự:

```text
Official API
↓
Nếu API không cung cấp tính năng cần thiết
↓
Browser Provider
```

Không giả định rằng tất cả platform đều có public search API.

Thiết kế:

```text
providers/
    douyin/
        official_api.py
        browser.py
        parser.py

    xiaohongshu/
        official_api.py
        browser.py
        parser.py
```

Provider phải có khả năng thay thế được.

Không được để toàn bộ project phụ thuộc vào selector HTML của một website.

---

# 4. YÊU CẦU AN TOÀN CHO BROWSER AUTOMATION

Browser automation chỉ phục vụ việc thực hiện thao tác tìm kiếm mà người dùng có thể thực hiện thủ công.

Không:

- bypass CAPTCHA;
- phá cơ chế anti-bot;
- tự động giải CAPTCHA;
- đánh cắp cookie;
- sử dụng cookie của người khác;
- bypass login;
- khai thác private API trái phép.

Nếu website yêu cầu CAPTCHA hoặc login:

```text
PAUSE JOB
↓
Thông báo trên UI
↓
Mở browser headed
↓
Người dùng tự xử lý
↓
Resume
```

Có thể sử dụng persistent browser profile của chính người dùng.

Tốc độ request/search phải hợp lý.

Không tạo crawler tốc độ cao.

Concurrency mặc định:

```text
1 provider = 1 browser worker
```

Có thể nâng lên sau.

---

# 5. TECH STACK

## Frontend

Sử dụng:

```text
Next.js
TypeScript
React
Tailwind CSS
shadcn/ui
```

Frontend chạy:

```text
localhost:3000
```

---

## Backend

Sử dụng:

```text
Python
FastAPI
Pydantic
SQLAlchemy
```

Backend:

```text
localhost:8000
```

---

## Browser automation

Sử dụng:

```text
Playwright Python Async API
```

Ưu tiên Chromium.

Hỗ trợ:

```text
Headed mode
Headless mode
Persistent browser context
```

Trong giai đoạn đầu mặc định:

```text
headed = true
```

để dễ login/debug.

Sau khi ổn định mới thêm headless.

---

# 6. DATABASE

V1 dùng:

```text
SQLite
```

Không cần PostgreSQL ngay.

Database file:

```text
data/app.db
```

Sau này phải có khả năng chuyển sang PostgreSQL.

---

# 7. CẤU TRÚC PROJECT

Tạo monorepo:

```text
ai-video-research-tool/

├── frontend/
│   ├── app/
│   ├── components/
│   ├── features/
│   │   ├── search/
│   │   ├── library/
│   │   ├── video/
│   │   └── settings/
│   ├── lib/
│   └── types/
│
├── backend/
│   ├── app/
│   │
│   ├── api/
│   │   ├── search.py
│   │   ├── videos.py
│   │   ├── collections.py
│   │   └── settings.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   └── database.py
│   │
│   ├── models/
│   │   ├── video.py
│   │   ├── search_job.py
│   │   ├── search_query.py
│   │   └── collection.py
│   │
│   ├── schemas/
│   │
│   ├── services/
│   │   ├── search_service.py
│   │   ├── ranking_service.py
│   │   ├── dedup_service.py
│   │   ├── ai_service.py
│   │   └── translation_service.py
│   │
│   ├── providers/
│   │   ├── base.py
│   │   │
│   │   ├── douyin/
│   │   │   ├── provider.py
│   │   │   ├── browser.py
│   │   │   └── parser.py
│   │   │
│   │   └── xiaohongshu/
│   │       ├── provider.py
│   │       ├── browser.py
│   │       └── parser.py
│   │
│   └── workers/
│       └── search_worker.py
│
├── data/
├── logs/
├── tests/
├── scripts/
│   ├── setup_windows.bat
│   ├── start.bat
│   └── stop.bat
│
├── .env.example
├── README.md
└── docker-compose.yml
```

Docker KHÔNG phải requirement cho V1.

Ưu tiên Windows native trước.

---

# 8. DATA MODEL — VIDEO

Tạo normalized Video model.

Các field nên có:

```text
id

platform

platform_video_id

url

share_url

thumbnail_url

title

caption

author_name

author_id

author_url

hashtags

keywords

duration_seconds

published_at

like_count

comment_count

share_count

favorite_count

view_count

search_query

search_job_id

relevance_score

quality_score

final_score

status

created_at

updated_at
```

Status:

```text
new
saved
skipped
used
favorite
```

Các field platform không cung cấp được thì để:

```text
NULL
```

Không fake dữ liệu.

---

# 9. SEARCH JOB MODEL

Mỗi lần bấm Search phải tạo một SearchJob.

Ví dụ:

```text
id

original_query

platforms

status

requested_limit

found_count

processed_count

created_at

started_at

completed_at

error_message
```

Status:

```text
pending
expanding_query
searching
ranking
completed
cancelled
failed
waiting_for_login
waiting_for_user
```

Frontend phải hiển thị trạng thái này.

---

# 10. AI QUERY ENGINE

Người dùng có thể nhập tiếng Việt.

Ví dụ:

```text
đồ decor bàn học cute
```

AI phải trả về structured JSON.

Schema:

```json
{
    "original_query": "đồ decor bàn học cute",

    "translated_query": "可爱书桌装饰",

    "primary_keywords": [
        "桌面装饰",
        "书桌布置"
    ],

    "related_keywords": [
        "桌面收纳",
        "桌面好物",
        "学生党好物",
        "宿舍好物"
    ],

    "hashtags": [
        "桌面布置",
        "桌面好物",
        "好物分享"
    ],

    "negative_keywords": [],

    "topics": [
        "desk decor",
        "cute products",
        "storage"
    ]
}
```

Dùng Pydantic validation.

Nếu AI trả JSON lỗi:

- retry tối đa 2 lần;
- nếu vẫn lỗi thì fallback sang original query.

Search không được chết chỉ vì AI lỗi.

---

# 11. AI PROVIDER

Không hardcode OpenAI vào business logic.

Tạo abstraction:

```text
AIProvider
```

Sau này có thể có:

```text
OpenAIProvider
GeminiProvider
LocalProvider
```

Settings:

```text
AI_PROVIDER=
AI_API_KEY=
AI_MODEL=
```

API key lưu ở:

```text
.env
```

Không commit API key.

---

# 12. SEARCH PIPELINE

Pipeline chuẩn:

```text
START SEARCH
      ↓
Create SearchJob
      ↓
Analyze user query
      ↓
Generate Chinese keywords
      ↓
Generate hashtags
      ↓
Create SearchQuery list
      ↓
Send queries to selected providers
      ↓
Collect raw results
      ↓
Normalize
      ↓
Deduplicate
      ↓
Calculate relevance
      ↓
Calculate quality
      ↓
Calculate final score
      ↓
Store DB
      ↓
Return results
```

Không bắt frontend phải chờ toàn bộ job hoàn tất mới thấy video.

Kết quả nên có thể hiển thị dần.

---

# 13. QUERY BUDGET

Không để AI tạo hàng trăm query.

Mặc định:

```text
Primary keywords: 5

Related keywords: 8

Hashtags: 8
```

Nhưng Search Service chỉ dùng tối đa:

```text
10 search queries / platform / job
```

Settings cho phép chỉnh sau.

---

# 14. DEDUPLICATION

V1 sử dụng:

### Level 1

```text
platform + platform_video_id
```

### Level 2

Canonical normalized URL.

### Level 3

Nếu platform_video_id không lấy được:

hash của:

```text
platform
author
caption
thumbnail
```

Không cần semantic duplicate detection ở V1.

Semantic duplicate để Phase sau.

---

# 15. RELEVANCE SCORE

Không trộn engagement với relevance.

Tạo:

```text
relevance_score
quality_score
final_score
```

---

## Relevance Score

0–100.

Dựa vào:

```text
caption similarity
title similarity
hashtag match
keyword match
semantic similarity
```

Ví dụ:

```text
semantic text similarity      50%
hashtag relevance             20%
keyword match                 20%
negative keyword penalty      10%
```

---

## Quality Score

Dựa trên:

```text
likes
comments
favorites
shares
freshness
```

Phải normalize.

Không để video triệu like luôn thắng video niche chỉ vì engagement cao.

---

## Final Score

Ban đầu:

```text
final_score =
0.80 * relevance_score
+
0.20 * quality_score
```

Cho phép cấu hình sau.

---

# 16. AI RANKING

Không gọi LLM một lần cho từng video nếu có 500 video.

Phải tránh:

```text
500 videos
=
500 API calls
```

V1:

Dùng text similarity + keyword scoring trước.

LLM chỉ sử dụng:

- query expansion;
- phân tích các video top candidate nếu cần.

Sau này có thể dùng embedding.

---

# 17. SEARCH RESULT UI

Trang Search:

```text
┌───────────────────────────────────────────┐
│ AI VIDEO RESEARCH                         │
├───────────────────────────────────────────┤
│                                           │
│ Bạn muốn tìm gì?                          │
│                                           │
│ [_______________________________]         │
│                                           │
│ Platforms                                 │
│ ☑ Douyin                                  │
│ ☑ Xiaohongshu                             │
│                                           │
│ Result limit                              │
│ [ 50 ▼ ]                                  │
│                                           │
│          [ 🔍 AI Search ]                 │
└───────────────────────────────────────────┘
```

---

# 18. SEARCH PROGRESS

Sau khi bấm search:

```text
Searching...

AI keywords generated ✓

Douyin:
32 videos

Xiaohongshu:
41 videos

Removing duplicates...
8 duplicates removed

Ranking...
54 / 65

[Cancel Search]
```

Không được để UI đứng im khiến người dùng không biết tool có chạy hay không.

Có progress indicator.

---

# 19. VIDEO CARD

Mỗi kết quả hiển thị:

```text
Thumbnail

Platform badge

Relevance:
94%

Title / Caption

Author

Likes

Comments

Favorites

Hashtags

Published date
```

Buttons:

```text
Open

Copy Link

Save

Skip
```

Click thumbnail:

mở original video trên platform.

KHÔNG cần download video trong V1.

---

# 20. FILTER

Toolbar:

```text
Platform

Minimum relevance

Minimum likes

Date

Status

Sort
```

Sort:

```text
Best Match
Most Likes
Newest
Most Comments
Most Favorites
```

Default:

```text
Best Match
```

---

# 21. LIBRARY

Tạo trang:

```text
/library
```

Các tab:

```text
Saved

Favorites

Used

Skipped

All
```

Người dùng có thể tìm lại video đã lưu.

Search trong Library:

```text
caption
hashtags
author
topic
```

---

# 22. COLLECTION

Cho phép tạo collection.

Ví dụ:

```text
BJD

Sticker

Unboxing

Cute products

DIY

Decor

Video chờ dịch

Video đã dùng
```

Một video có thể thuộc nhiều collection.

---

# 23. DETAILS MODAL

Click một video mở panel/modal.

Hiển thị:

```text
Thumbnail

Caption đầy đủ

Hashtags

Author

Engagement

Search query đã tìm ra nó

Related keywords

Relevance score

Quality score

Final score

URL
```

Actions:

```text
Open Original

Copy URL

Save

Add Collection

Mark Used

Skip
```

---

# 24. SETTINGS PAGE

Tạo:

```text
/settings
```

Sections:

### AI

```text
Provider

API key

Model

Test connection
```

### Search

```text
default result limit

max queries

minimum relevance

search timeout
```

### Browser

```text
Headed / Headless

Browser profile directory

Open browser login
```

### Database

```text
DB location

Export data
```

---

# 25. LOGIN MANAGEMENT

Tạo nút:

```text
Open Douyin Login Browser

Open Xiaohongshu Login Browser
```

Mở persistent browser context.

Người dùng tự login.

Không tự thu thập username/password.

Sau login:

```text
Login session detected ✓
```

Nếu session hết hạn:

```text
Login required
[Open Browser]
```

---

# 26. ERROR HANDLING

Mỗi provider phải phân biệt:

```text
LoginRequiredError

CaptchaRequiredError

RateLimitedError

SelectorChangedError

NetworkError

TimeoutError

ProviderUnavailableError
```

Không return:

```text
Something went wrong
```

nếu có thể xác định nguyên nhân.

Frontend hiển thị:

```text
Xiaohongshu cần đăng nhập lại.

[Open Login Browser]
```

hoặc:

```text
Douyin search page structure may have changed.

View logs
```

---

# 27. LOGGING

Logs:

```text
logs/app.log
```

Mỗi log search cần:

```text
timestamp
job_id
provider
query
level
message
```

Ví dụ:

```text
[INFO]
job=abc123
provider=douyin
query=手帐工具
results=18
```

Không log:

```text
password
API key
private cookies
```

---

# 28. DEVELOPMENT MODE

Tạo MockProvider.

```text
providers/mock/
```

MockProvider trả về fake sample data.

Điều này cực kỳ quan trọng.

Frontend và pipeline phải có thể được phát triển mà KHÔNG cần Douyin/Xiaohongshu hoạt động.

Environment:

```text
USE_MOCK_PROVIDER=true
```

---

# 29. API ENDPOINTS

Thiết kế REST API ít nhất gồm:

```text
POST /api/search

GET /api/search/jobs/{id}

POST /api/search/jobs/{id}/cancel

GET /api/videos

GET /api/videos/{id}

PATCH /api/videos/{id}

POST /api/videos/{id}/save

POST /api/videos/{id}/skip

GET /api/collections

POST /api/collections

POST /api/collections/{id}/videos

GET /api/settings

PATCH /api/settings

GET /api/health
```

Có OpenAPI docs mặc định của FastAPI.

---

# 30. REALTIME PROGRESS

Ưu tiên đơn giản.

Có thể sử dụng:

```text
polling
```

trước.

Ví dụ frontend:

```text
GET job status mỗi 1 giây
```

Không cần WebSocket ngay.

WebSocket/SSE để Phase sau.

---

# 31. EXPORT

Cho phép export kết quả thành:

```text
CSV
JSON
```

Fields CSV:

```text
platform
url
caption
author
hashtags
likes
comments
favorites
published_at
relevance_score
status
```

Không cần Excel riêng trong V1.

---

# 32. PERFORMANCE

Mục tiêu V1:

```text
50–200 search results/job
```

Không thiết kế crawler hàng trăm nghìn video.

Database pagination.

Frontend không render 5.000 card cùng lúc.

---

# 33. PHASE 0 — PROJECT FOUNDATION

Làm trước:

- tạo monorepo;
- Next.js frontend;
- FastAPI backend;
- SQLite;
- SQLAlchemy models;
- config;
- `.env`;
- logging;
- health endpoint;
- MockProvider;
- SearchJob;
- Video model.

Acceptance criteria:

```text
scripts/setup_windows.bat
```

setup được project.

Sau đó:

```text
scripts/start.bat
```

khởi động:

Frontend + Backend.

Browser mở:

```text
http://localhost:3000
```

Trang health phải hiển thị backend online.

---

# 34. PHASE 1 — SEARCH UI + MOCK PIPELINE

Chưa cần Douyin/Xiaohongshu thật.

Làm hoàn chỉnh:

```text
Search UI
↓
Create job
↓
MockProvider
↓
Normalize
↓
Deduplicate
↓
Ranking
↓
Database
↓
Result cards
↓
Save / Skip
```

Acceptance:

Người dùng nhập:

```text
unbox đồ cute
```

bấm Search.

Tool trả sample results.

Có:

```text
progress
filter
sort
save
skip
library
```

Nếu Phase này chưa ổn:

KHÔNG sang Phase 2.

---

# 35. PHASE 2 — AI QUERY EXPANSION

Thêm AIProvider.

Workflow:

```text
Vietnamese query
↓
AI
↓
Chinese keywords
↓
hashtags
↓
related topics
```

UI hiển thị:

```text
AI generated:

可爱开箱
少女心好物
好物分享
小众好物
```

Có thể cho người dùng:

```text
☑ keyword 1
☑ keyword 2
☑ keyword 3
```

trước khi Search.

Có Advanced Mode để edit query.

---

# 36. PHASE 3 — XIAOHONGSHU PROVIDER

Chỉ triển khai một platform thật trước.

Ưu tiên Xiaohongshu hoặc platform nào dễ test ổn định hơn tại thời điểm triển khai.

Provider phải:

```text
open platform search

enter keyword

read visible search results

extract normalized metadata

paginate / scroll có giới hạn

return VideoResult
```

Không download video.

Nếu login:

```text
waiting_for_login
```

Nếu CAPTCHA:

```text
waiting_for_user
```

Acceptance:

Search một keyword tiếng Trung.

Lấy được tối thiểu một tập kết quả thực tế và lưu vào SQLite.

---

# 37. PHASE 4 — DOUYIN PROVIDER

Implement cùng interface.

Ưu tiên official API nếu tài khoản/application có quyền phù hợp.

Nếu không:

browser provider.

Không thay đổi SearchService.

Acceptance:

```text
platform=douyin
```

trả về VideoResult cùng schema với Xiaohongshu.

Frontend không cần biết data đến từ phương pháp nào.

---

# 38. PHASE 5 — MULTI-PLATFORM SEARCH

Cho phép:

```text
☑ Douyin
☑ Xiaohongshu
```

SearchService chạy lần lượt hoặc concurrency thấp.

Merge:

```text
Douyin results
+
Xiaohongshu results
```

Sau đó:

```text
deduplicate
rank
display
```

Hiển thị platform badge.

---

# 39. PHASE 6 — FIND SIMILAR

Đây là feature rất quan trọng.

Trên Video Card có:

```text
Find Similar
```

Khi bấm:

Tool lấy:

```text
caption
title
hashtags
topics
```

AI phân tích video đó.

Tạo:

```text
similar keywords

similar hashtags

related concepts
```

Sau đó chạy search mới.

Ví dụ:

```text
Video gốc
↓
可爱桌面收纳
↓
AI expansion
↓
Douyin + RED
↓
50 video tương tự
```

Tạo relation:

```text
source_video_id
similar_search_job_id
```

---

# 40. PHASE 7 — SEMANTIC SEARCH

Sau khi V1 ổn định mới thêm embeddings.

Có thể dùng:

```text
local embeddings
```

hoặc API.

Vector hóa:

```text
caption
hashtags
title
```

Sau đó hỗ trợ:

```text
semantic similarity
```

Không cần vector database ngay nếu dataset nhỏ.

SQLite + local vector storage có thể đủ ở giai đoạn đầu.

Nếu dữ liệu lớn mới cân nhắc:

```text
Qdrant
pgvector
```

---

# 41. PHASE 8 — VIDEO UNDERSTANDING

KHÔNG triển khai ở MVP.

Sau này thêm:

```text
audio transcript

OCR

frame analysis

visual description
```

Pipeline:

```text
video
↓
audio
↓
speech-to-text

frames
↓
OCR / vision

caption
+
transcript
+
OCR
+
visual
↓
AI understanding
```

Sau đó người dùng có thể tìm:

```text
video unbox một món đồ màu hồng dùng để decor bàn
```

ngay cả khi caption không có từ chính xác đó.

---

# 42. PHASE 9 — TREND DISCOVERY

Không làm ngay.

Sau này SearchJob có thể chạy định kỳ.

Lưu historical metrics.

Phân tích:

```text
keyword frequency

hashtag frequency

engagement growth

topic growth

new product trends
```

Dashboard:

```text
Trending 7 days

撕膜神器       +230%

桌面收纳       +180%

BJD开箱        +140%
```

---

# 43. KHÔNG LÀM TRONG MVP

Không triển khai các phần sau ngay:

```text
video downloader

auto repost

auto upload

mass scraping

proxy rotation

captcha bypass

account farming

cloud deployment

mobile app

Docker production

Redis

Celery

Kafka

microservices

Kubernetes
```

Những thứ này chỉ làm project phức tạp.

---

# 44. CODE QUALITY

Yêu cầu:

- type hints;
- Pydantic schemas;
- không dùng giant files;
- không tạo file Python > khoảng 500–700 dòng nếu có thể tách;
- service/provider rõ trách nhiệm;
- tránh duplicate code;
- centralized config;
- centralized error handling;
- meaningful naming;
- async I/O ở browser/network layer.

Không over-engineering.

---

# 45. TEST

Backend:

```text
pytest
```

Ít nhất test:

```text
query expansion parser

normalizer

deduplication

relevance score

final ranking

database CRUD

MockProvider

SearchService
```

Frontend:

test critical components nếu hợp lý.

Playwright E2E để sau khi UI ổn định.

---

# 46. E2E TEST

Critical E2E:

```text
Open app
↓
Enter query
↓
Start mock search
↓
Wait job complete
↓
Result appears
↓
Save video
↓
Open Library
↓
Saved video exists
```

Test này phải chạy được mà không cần Douyin/Xiaohongshu.

---

# 47. SETUP WINDOWS

Tôi sử dụng Windows.

Tạo:

```text
scripts/setup_windows.bat
```

Nó cần kiểm tra:

```text
Python
Node.js
npm/pnpm
```

Cài:

```text
frontend dependencies
backend dependencies
Playwright Chromium
```

Không được xóa hoặc ghi đè phần mềm hệ thống.

---

# 48. START SCRIPT

Tạo:

```text
start.bat
```

Người dùng double click.

Tool:

```text
check backend

start FastAPI

start Next.js

wait

open localhost:3000
```

Nếu port đang dùng:

hiển thị lỗi dễ hiểu.

---

# 49. STOP SCRIPT

Tạo:

```text
stop.bat
```

Chỉ stop process của project.

Không được kill tất cả Python/Node process trên máy.

---

# 50. README

README phải viết rõ từng bước cho người không biết code.

Bao gồm:

```text
1. Cài đặt

2. Chạy tool

3. Stop tool

4. Cấu hình AI

5. Login Douyin

6. Login Xiaohongshu

7. Search video

8. Save video

9. Troubleshooting
```

Nếu có command:

đưa command copy/paste đầy đủ.

---

# 51. DEV DOCUMENTATION

Tạo:

```text
docs/
    ARCHITECTURE.md
    DATABASE.md
    PROVIDERS.md
    SEARCH_PIPELINE.md
    ROADMAP.md
```

`ROADMAP.md` dùng checkbox:

```text
[x] Phase 0
[x] Phase 1
[ ] Phase 2
[ ] Phase 3
...
```

Cập nhật khi hoàn thành.

---

# 52. QUY TẮC LÀM VIỆC CHO CODEX

Rất quan trọng.

Không cố implement toàn bộ project trong một lần.

Thực hiện từng phase.

Trước khi sửa:

1. inspect codebase;
2. đọc README;
3. đọc ROADMAP;
4. xác định phase hiện tại.

Sau mỗi phase:

1. chạy lint;
2. chạy type check;
3. chạy backend tests;
4. chạy frontend build;
5. chạy E2E nếu có;
6. sửa lỗi phát hiện;
7. cập nhật ROADMAP;
8. cập nhật README nếu cần.

Không tuyên bố "hoàn thành" nếu test đang fail.

---

# 53. DEBUGGING

Nếu provider fail vì website thay đổi:

Không sửa lung tung toàn project.

Chỉ inspect:

```text
provider browser layer
parser
selectors
```

Core SearchService phải tiếp tục hoạt động.

Provider phải log đủ để debug.

Có thể lưu:

```text
debug screenshot
```

khi parsing thất bại.

Directory:

```text
logs/debug/
```

Nhưng không lưu thông tin nhạy cảm không cần thiết.

---

# 54. USER EXPERIENCE

Đây là tool dành cho người không chuyên code.

Ưu tiên:

```text
button
status
progress
error message
```

thay cho bắt người dùng mở terminal.

UI nên sạch, hiện đại, kiểu dashboard.

Sidebar:

```text
Search

Library

Collections

History

Settings
```

---

# 55. DASHBOARD

V1 dashboard đơn giản:

```text
Total videos

Saved

Used

Search jobs
```

Recent searches.

Ví dụ:

```text
unbox đồ cute

BJD makeup

sticker tools

desk decor
```

Click recent search mở lại kết quả.

---

# 56. SEARCH HISTORY

Không mất search cũ khi restart.

Trang:

```text
/history
```

Mỗi job:

```text
Query

Date

Platforms

Videos found

Status
```

Có:

```text
Open Results

Run Again

Delete
```

---

# 57. CACHING

Nếu cùng một video đã tồn tại:

Không tạo duplicate database row.

Update metadata nếu dữ liệu mới hơn.

Ví dụ:

```text
like_count
comment_count
favorite_count
```

có thể update.

---

# 58. SEARCH AGAIN

Nếu user chạy cùng query:

Không chỉ trả DB cache.

Có option:

```text
Search fresh results
```

nhưng deduplicate với existing database.

---

# 59. CANCEL

User phải có thể bấm:

```text
Cancel Search
```

Worker dừng sạch.

Browser không bị treo.

SearchJob:

```text
cancelled
```

Không làm database corrupt.

---

# 60. PROVIDER CONTRACT

Tất cả platform phải trả normalized result dạng tương tự:

```python
class VideoResult(BaseModel):

    platform: str

    platform_video_id: str | None

    url: str

    thumbnail_url: str | None

    title: str | None

    caption: str | None

    author_name: str | None

    author_id: str | None

    hashtags: list[str]

    duration_seconds: float | None

    published_at: datetime | None

    like_count: int | None

    comment_count: int | None

    share_count: int | None

    favorite_count: int | None

    view_count: int | None
```

Frontend không sử dụng raw platform payload.

---

# 61. RAW DATA

Nếu cần debug, có thể lưu raw provider payload.

Tạo field/table riêng.

Không trộn raw JSON vào Video model chính.

Ví dụ:

```text
video_source_data
```

---

# 62. CONFIGURATION

Tạo `.env.example`.

Ví dụ:

```text
APP_ENV=development

DATABASE_URL=sqlite:///./data/app.db

AI_PROVIDER=
AI_API_KEY=
AI_MODEL=

PLAYWRIGHT_HEADLESS=false

DOUYIN_PROFILE_DIR=./data/browser/douyin

XHS_PROFILE_DIR=./data/browser/xiaohongshu
```

Không commit `.env`.

---

# 63. GITIGNORE

Phải ignore:

```text
.env

data/browser/

data/app.db

logs/

node_modules/

.next/

__pycache__/

.pytest_cache/
```

Không commit browser profile.

---

# 64. SECURITY

Không expose backend ra Internet mặc định.

Bind:

```text
127.0.0.1
```

thay vì:

```text
0.0.0.0
```

Tool chỉ chạy local.

---

# 65. MVP DEFINITION OF DONE

MVP chỉ được coi là hoàn thành khi:

### Setup

```text
setup_windows.bat
```

hoạt động.

### Start

```text
start.bat
```

mở app.

### Search

Người dùng nhập tiếng Việt.

### AI

Tool tạo keyword tiếng Trung.

### Platform

Ít nhất một real provider hoạt động.

### Database

Video lưu được.

### Ranking

Kết quả có relevance score.

### UI

Có:

```text
Search
Results
Filter
Save
Skip
Library
History
Settings
```

### Stability

Restart tool không mất dữ liệu.

### Testing

Critical tests pass.

### Documentation

README đầy đủ.

---

# 66. SAU MVP

Thứ tự phát triển tiếp:

```text
MVP
↓
Platform thứ hai
↓
Find Similar
↓
Embeddings
↓
Semantic Search
↓
Transcript
↓
OCR
↓
Visual AI
↓
Trend Detection
↓
Scheduled Research Agent
```

Không đổi thứ tự nếu không có lý do rõ ràng.

---

# 67. UI DESIGN DIRECTION

Style:

```text
modern
clean
minimal
content research dashboard
```

Ưu tiên dark/light mode.

Cards không quá nhiều text.

Thumbnail phải là yếu tố chính.

Desktop-first vì tôi dùng Windows.

Responsive nhưng chưa cần tối ưu mobile hoàn hảo.

---

# 68. TÍNH NĂNG QUAN TRỌNG NHẤT

Thứ tự ưu tiên sản phẩm:

```text
1. Search đúng video

2. AI keyword expansion

3. Relevance ranking

4. Save / organize

5. Find Similar

6. Video understanding

7. Trends
```

Không hy sinh Search quality để làm feature phụ.

---

# 69. NGUYÊN TẮC QUAN TRỌNG

Nếu một feature chưa chắc làm được do hạn chế của Douyin/Xiaohongshu:

Không fake.

Không hardcode dummy result rồi tuyên bố hoạt động.

Hãy:

1. ghi rõ limitation;
2. isolate feature;
3. giữ provider interface;
4. tiếp tục làm phần còn lại;
5. ghi issue vào ROADMAP.

---

# 70. VIỆC BẠN PHẢI LÀM NGAY BÂY GIỜ

Bây giờ KHÔNG triển khai toàn bộ các Phase.

Hãy bắt đầu bằng:

## STEP 1

Inspect workspace hiện tại.

Nếu project chưa tồn tại:

tạo project:

```text
ai-video-research-tool
```

## STEP 2

Tạo:

```text
docs/ARCHITECTURE.md

docs/ROADMAP.md
```

dựa trên specification này.

## STEP 3

Implement:

```text
PHASE 0
```

Project foundation.

## STEP 4

Implement:

```text
PHASE 1
```

Mock Search Pipeline hoàn chỉnh.

## STEP 5

Chạy toàn bộ test/build.

## STEP 6

Khởi động app thực tế và kiểm tra E2E:

```text
Search
→
Results
→
Save
→
Library
```

## STEP 7

Nếu tất cả hoạt động:

Cập nhật:

```text
ROADMAP.md
```

và báo cho tôi:

- phần nào đã hoàn thành;
- test nào đã chạy;
- file nào quan trọng;
- command/start script để tôi mở tool;
- lỗi hoặc limitation còn lại;
- Phase tiếp theo nên làm gì.

KHÔNG tự động nhảy sang browser scraping thực tế trước khi nền móng và Mock Search Pipeline hoạt động ổn định.

Hãy bắt đầu thực hiện Phase 0 và Phase 1 ngay.