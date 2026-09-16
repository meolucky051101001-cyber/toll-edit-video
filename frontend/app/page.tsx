import { Suspense } from "react";
import { SearchWorkspace } from "@/features/search/search-workspace";
export default function Page() {
  return (
    <Suspense fallback={<p>Đang tải workspace…</p>}>
      <SearchWorkspace />
    </Suspense>
  );
}
