import { test, expect } from "@playwright/test";

test("mock search → results → save → library → reload; filters and skip", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await page
    .getByRole("checkbox", { name: "Tự tạo từ khóa AI khi tìm" })
    .uncheck();
  await expect(page.getByText("Backend online")).toBeVisible();
  await page
    .getByRole("textbox", { name: "Chủ đề tìm kiếm" })
    .fill("unbox đồ cute");
  await page
    .getByRole("combobox", { name: "Số kết quả", exact: true })
    .selectOption("20");
  await page
    .getByRole("button", { name: "Tìm video mẫu", exact: true })
    .click();
  await expect(page.getByText("Hoàn tất", { exact: true })).toBeVisible();
  const cards = page.getByTestId("video-card");
  await expect(cards).toHaveCount(20);
  const first = cards.first();
  const title = await first.locator(".card-title").innerText();
  // Ensure this test works on repeat without deleting the user's workspace.
  if (
    await first.getByRole("button", { name: "Đã lưu", exact: true }).count()
  ) {
    await first.getByRole("button", { name: "Đã lưu", exact: true }).click();
  }
  await first.getByRole("button", { name: "Lưu", exact: true }).click();
  await expect(
    first.getByRole("button", { name: "Đã lưu", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Thư viện", exact: true }).click();
  await expect(
    page.getByTestId("video-card").filter({ hasText: title }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByTestId("video-card").filter({ hasText: title }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Tất cả", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Lọc nền tảng" })
    .selectOption("xiaohongshu");
  await expect(page.locator(".platform-badge.douyin")).toHaveCount(0);
  const redCard = page
    .getByTestId("video-card")
    .filter({
      hasNot: page.getByRole("button", { name: "Đã lưu", exact: true }),
    })
    .first();
  await redCard
    .getByRole("button", { name: "Bỏ qua video", exact: true })
    .click();
  await page.getByRole("button", { name: "Bỏ qua", exact: true }).click();
  await expect(page.getByTestId("video-card").first()).toBeVisible();
  const response = await request.get("/api/videos?status=saved");
  expect(response.ok()).toBeTruthy();
  expect((await response.json()).total).toBeGreaterThan(0);
});

test("cancel a running job", async ({ page }) => {
  await page.goto("/");
  await page
    .getByRole("checkbox", { name: "Tự tạo từ khóa AI khi tìm" })
    .uncheck();
  await page
    .getByRole("textbox", { name: "Chủ đề tìm kiếm" })
    .fill("cancel test");
  await page
    .getByRole("combobox", { name: "Số kết quả", exact: true })
    .selectOption("200");
  await page
    .getByRole("button", { name: "Tìm video mẫu", exact: true })
    .click();
  await page.getByRole("button", { name: "Hủy tìm kiếm" }).click();
  await expect(page.getByText("Đã hủy", { exact: true })).toBeVisible();
});

test("preview fixture → edit queries → persisted manual search; invalidates stale preview", async ({
  page,
}) => {
  await page.route("**/api/search/expand", async (route) => {
    const query = route.request().postDataJSON().query;
    await route.fulfill({
      json: {
        original_query: query,
        source: "gemini",
        cached: false,
        warning: null,
        queries: ["可爱开箱", "文具开箱"],
        expansion: {
          original_query: query,
          translated_query: "可爱开箱",
          primary_keywords: ["文具开箱"],
          related_keywords: ["萌物"],
          hashtags: ["开箱"],
          negative_keywords: [],
          topics: ["文具"],
        },
      },
    });
  });
  await page.goto("/");
  const topic = page.getByRole("textbox", { name: "Chủ đề tìm kiếm" });
  await topic.fill("AI UI test");
  await page
    .getByRole("button", { name: "Tạo từ khóa AI", exact: true })
    .click();
  await expect(page.locator(".ai-translation")).toHaveText("可爱开箱");
  await page
    .getByRole("textbox", { name: "Từ khóa tìm kiếm", exact: true })
    .fill("可爱开箱\n文具开箱");
  await page
    .getByRole("button", { name: "Tìm video mẫu", exact: true })
    .click();
  await expect(page.getByText("Hoàn tất", { exact: true })).toBeVisible();
  await expect(page.locator(".job-queries")).toContainText("文具开箱");
  await page.reload();
  await expect(page.locator(".job-queries")).toContainText("可爱开箱");
  await topic.fill("Another topic");
  await expect(page.locator(".ai-translation")).toHaveCount(0);
});
