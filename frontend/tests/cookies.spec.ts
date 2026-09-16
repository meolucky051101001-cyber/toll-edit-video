import { test, expect } from "@playwright/test";

test.describe("Cookie Settings & Anti-Bot Resolution UI", () => {
  test("renders cookie settings panel with platform cards, tokens and sync actions", async ({
    page,
  }) => {
    await page.goto("/settings");

    // Check heading
    await expect(
      page.getByRole("heading", {
        name: "Cấu hình Cookie & Tải Video Chất Lượng Cao (1080p / Gốc)",
      })
    ).toBeVisible({ timeout: 10000 });

    // Check notice banner
    await expect(
      page.getByText("Cơ chế bảo vệ bản quyền & độ phân giải tối đa (1080p / Gốc)")
    ).toBeVisible();

    // Check Xiaohongshu platform card
    await expect(page.getByText("Xiaohongshu / RedNote")).toBeVisible();
    await expect(page.locator(".token-pill", { hasText: "web_session" })).toBeVisible();
    await expect(page.locator(".token-pill", { hasText: "a1" })).toBeVisible();
    await expect(page.locator(".token-pill", { hasText: "webId" })).toBeVisible();

    // Check Douyin platform card
    await expect(page.getByText("Douyin (抖音)")).toBeVisible();
    await expect(page.locator(".token-pill", { hasText: "ttwid" })).toBeVisible();
    await expect(page.locator(".token-pill", { hasText: "s_v_web_id" })).toBeVisible();
    await expect(page.locator(".token-pill", { hasText: "sessionid" })).toBeVisible();

    // Check global sync button
    await expect(
      page.getByRole("button", {
        name: "Đồng bộ từ trình duyệt (Cả 2 nền tảng)",
      })
    ).toBeVisible();

    // Test toggle manual input for Xiaohongshu
    const manualBtnXhs = page.getByRole("button", {
      name: "Dán Cookie thủ công",
    }).first();
    await manualBtnXhs.click();
    await expect(
      page.locator("#xhs-cookie-input")
    ).toBeVisible();

    // Close manual input
    await page.getByRole("button", { name: "Đóng nhập" }).click();
    await expect(page.locator("#xhs-cookie-input")).not.toBeVisible();
  });
});
