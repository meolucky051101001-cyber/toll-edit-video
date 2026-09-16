import { test, expect } from "@playwright/test";

test("import preview and confirmation, provenance card, file change resets preview", async ({
  page,
}) => {
  const fixture = JSON.stringify([
    {
      note_id: "aaaaaaaaaaaaaaaaaaaaaaaa",
      type: "video",
      title: "Synthetic UI fixture",
    },
  ]);
  const summary = {
    total: 2,
    accepted: 1,
    existing: 0,
    new: 1,
    skipped: 1,
    issues: [{ row: 2, reason: "Bỏ qua bài ảnh." }],
    sample: [
      {
        title: "Synthetic UI fixture",
        platform: "xiaohongshu",
        like_count: null,
      },
    ],
  };
  let committed = 0;
  await page.route("**/api/imports/mediacrawler/*", async (route) => {
    expect(route.request().postDataJSON().content).toBe(fixture);
    if (route.request().url().endsWith("/commit")) {
      committed++;
      await route.fulfill({
        status: 201,
        json: { ...summary, job_id: "fixture-only", imported: 1 },
      });
    } else await route.fulfill({ json: summary });
  });
  await page.route("**/api/videos?**", async (route) =>
    route.fulfill({
      json: {
        total: 1,
        page: 1,
        items: [
          {
            id: "fixture-only",
            platform: "xiaohongshu",
            url: "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa",
            title: "Synthetic UI fixture",
            caption: "Test-only metadata",
            thumbnail_url: null,
            author_name: null,
            hashtags: [],
            keywords: [],
            like_count: null,
            comment_count: null,
            favorite_count: null,
            share_count: null,
            duration_seconds: null,
            published_at: null,
            relevance_score: 0,
            quality_score: 0,
            final_score: 0,
            status: "new",
            is_mock: false,
            imported_from: "mediacrawler",
            search_query: "Test",
            search_job_id: "fixture-only",
          },
        ],
      },
    }),
  );
  await page.goto("/imports");
  const file = page.getByLabel("File MediaCrawler", { exact: true });
  await file.setInputFiles({
    name: "test-content.json",
    mimeType: "application/json",
    buffer: Buffer.from(fixture),
  });
  await page.getByRole("button", { name: "Xem trước dữ liệu" }).click();
  await expect(
    page.getByRole("heading", { name: "Kết quả kiểm tra" }),
  ).toBeVisible();
  expect(committed).toBe(0);
  await page.getByText("1 bản ghi bị bỏ qua", { exact: true }).click();
  await expect(page.getByText("Bản ghi 2: Bỏ qua bài ảnh.")).toBeVisible();
  await file.setInputFiles({
    name: "changed.json",
    mimeType: "application/json",
    buffer: Buffer.from(fixture),
  });
  await expect(
    page.getByRole("button", { name: "Nhập 1 video vào thư viện" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Xem trước dữ liệu" }).click();
  await page.getByRole("button", { name: "Nhập 1 video vào thư viện" }).click();
  await expect(
    page.getByRole("heading", { name: "Đã nhập metadata" }),
  ).toBeVisible();
  expect(committed).toBe(1);
  const card = page.getByTestId("video-card");
  await expect(card).toContainText("NHẬP MEDIACRAWLER");
  await expect(card).not.toContainText("MOCK PREVIEW");
  await expect(card.locator(".duration")).toHaveCount(0);
  await card.getByRole("button", { name: "Mở chi tiết" }).click();
  await expect(page.getByRole("link", { name: "Mở bản gốc" })).toBeVisible();
  await expect(page.getByText(/Metadata được nhập từ file/)).toBeVisible();
});
