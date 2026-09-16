import { VideoGrid } from "@/features/video/video-grid";
export default function Page() {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Những ý tưởng đáng giữ.</h1>
          <p>Video đã lưu vẫn ở đây sau khi bạn đóng ứng dụng.</p>
        </div>
      </div>
      <VideoGrid library />
    </>
  );
}
