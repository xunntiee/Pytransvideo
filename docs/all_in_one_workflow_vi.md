# Quy trình all-in-one: tải, xử lý, lồng tiếng và reup

## Chạy ứng dụng

Từ PowerShell tại thư mục dự án:

```powershell
.\.venv\Scripts\python.exe sp.py
```

## Luồng sử dụng nhanh

1. Mở **Tải video**.
2. Dán một hoặc nhiều URL hoặc nguyên văn đoạn share text.
3. Chọn cookie trình duyệt/file cookie nếu nền tảng yêu cầu đăng nhập.
4. Tải xong, bấm **Đưa video đã tải vào pipeline**.
5. Trong batch workflow:
   - chọn STT/model/CUDA và diarization nếu cần;
   - chọn dịch API/LLM hoặc Gemini qua Chrome;
   - bật edit, xóa/blur phụ đề cứng và burn phụ đề mới;
   - chọn TTS, giọng đọc, đồng bộ thời lượng và trộn âm thanh;
   - bật tạo bản 9:16 cho Shorts/TikTok/Douyin/Kuaishou/Xiaohongshu/Reels;
   - bật chuẩn bị reup và chọn các nền tảng.
6. Chạy batch. Mỗi stage được checkpoint trong
   `batch_vietsub_plan.json`, có thể chạy tiếp sau khi dừng hoặc lỗi.
7. Mở **Quản lý reup** để stage qua Chrome hoặc dùng API chính thức.

## Nguồn tải đã nhận diện

Registry hiện có adapter/header cho:

- Douyin
- Kuaishou
- Bilibili
- Xiaohongshu
- Weibo
- Xigua và Toutiao
- Youku
- iQiyi
- Tencent Video
- Mango TV
- AcFun
- Haokan
- Sohu
- Pear Video
- Douyu
- Huya
- YouTube, TikTok và URL phổ thông

Tool dùng downloader native cho Douyin khi có thể và fallback sang
`yt-dlp`. Các nền tảng có đăng nhập, chống bot, giới hạn vùng hoặc DRM vẫn
cần cookie/tài khoản hợp lệ; DRM không bị vượt qua.

## Reup an toàn

Queue nằm tại `<output>/reup_queue.json`.

- Mỗi video được hash toàn bộ bằng SHA-256 để tránh đăng trùng.
- Mỗi platform có trạng thái riêng:
  `ready`, `publishing`, `processing`, `staged`, `published`, `failed`,
  `skipped`.
- Browser staging chỉ chọn file và điền metadata. Tool không tự bấm nút
  xuất bản cuối cùng.
- Direct API luôn có hộp xác nhận cuối và mặc định là **Không**.

### Browser staging

Đã có trang/adapter cho:

- YouTube Studio
- TikTok Studio
- Douyin Creator
- Bilibili Studio
- Kuaishou Creator
- Xiaohongshu Creator
- Meta Business Suite

Chrome dùng profile bền vững của tool để giữ phiên đăng nhập.

## YouTube Direct API

1. Tạo OAuth Client kiểu **Desktop app** trong Google Cloud.
2. Bật YouTube Data API v3 và cấp scope `youtube.upload`.
3. Trong queue, chọn row YouTube rồi bấm **Đăng YouTube qua API**.
4. Lần đầu chọn file OAuth client-secret JSON; trình duyệt sẽ mở để chọn
   kênh và cấp quyền.
5. Phiên OAuth được lưu tại:
   `<output>/.credentials/youtube_token.json`.
6. Chọn privacy. Mặc định là `private`.
7. Xác nhận cờ nội dung tổng hợp/AI và xác nhận publish thật.

Có thể truyền access token tạm thời qua biến môi trường:

```powershell
$env:PYVIDEOTRANS_YOUTUBE_TOKEN = "access-token"
```

Upload sử dụng resumable session, chia chunk, theo dõi offset, retry lỗi
408/429/5xx và cập nhật URL video vào queue.

## TikTok Direct Post API

TikTok app phải được cấp scope `video.publish`. Client chưa được TikTok
audit có thể bị giới hạn chỉ đăng private.

Đặt user access token cho phiên chạy:

```powershell
$env:PYVIDEOTRANS_TIKTOK_TOKEN = "user-access-token"
```

Sau đó:

1. Chọn row TikTok và bấm **Đăng TikTok qua API**.
2. Tool query creator info mới nhất.
3. Kiểm tra đúng nickname/username, privacy được tài khoản hỗ trợ, giới hạn
   thời lượng và quyền comment/Duet/Stitch.
4. Giữ hoặc chỉnh cờ AIGC, nội dung thương hiệu.
5. Xác nhận publish thật.
6. Nếu queue ở trạng thái `processing`, bấm
   **Cập nhật trạng thái API** để đồng bộ thành `published` hoặc `failed`.

TikTok upload tuân theo giới hạn chunk 5–64 MB, final chunk tối đa 128 MB,
upload tuần tự và retry lỗi tạm thời.

## Giới hạn thực tế

- Không nền tảng nào bảo đảm tải được “mọi video”: video DRM, private,
  trả phí, giới hạn vùng hoặc tài khoản không có quyền sẽ bị từ chối.
- Không tự động vượt captcha, DRM hoặc cơ chế kiểm soát truy cập.
- Douyin/Bilibili/Kuaishou/Xiaohongshu hiện dùng browser staging để người
  dùng kiểm tra và bấm xuất bản cuối.
- Chỉ reup nội dung bạn sở hữu hoặc có quyền sử dụng; giữ nguồn gốc URL
  trong queue để truy vết.
