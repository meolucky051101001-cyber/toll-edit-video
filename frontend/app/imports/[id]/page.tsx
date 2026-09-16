import { VideoGrid } from "@/features/video/video-grid";
export default async function ImportResult({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Video từ file đã nhập</h1>
          <p>Metadata do bạn cung cấp, chưa được xác minh lại trên nền tảng.</p>
        </div>
      </div>
      <VideoGrid jobId={id} />
    </>
  );
}
