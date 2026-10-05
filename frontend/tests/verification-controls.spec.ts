import { test, expect } from "@playwright/test";

for (const platform of ["douyin", "xiaohongshu"]) {
  test(`${platform} challenge shows verification preview and refresh instead of QR login`, async ({ page }) => {
    let refreshCount = 0;
    const job = {
      id: "verification-controls-fixture", original_query: "decor bàn học",
      platforms: [platform], active_platform: platform, waiting_reason: "verification_required",
      status: "waiting_for_user", requested_limit: 5, processed_count: 0, found_count: 0,
      duplicate_count: 0, rejected_count: 0, provider_counts: {}, diagnostics: {},
      created_at: new Date().toISOString(), is_mock: false, queries: ["书桌布置"],
      error_message: "Nền tảng yêu cầu xác minh bảo mật.",
    };
    await page.route("**/api/search/jobs/verification-controls-fixture**", (route) => route.fulfill({ json: job }));
    await page.route("**/api/videos?**", (route) => route.fulfill({ json: { items: [], total: 0, page: 1 } }));
    await page.route(`**/api/browser/${platform}**`, async (route) => {
      if (route.request().url().includes("/refresh_verification")) refreshCount++;
      if (route.request().url().includes("/screenshot")) {
        await route.fulfill({ contentType: "image/png", body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+juc0AAAAASUVORK5CYII=", "base64") });
        return;
      }
      await route.fulfill({ json: {
        state: "verification_required", message: "Cần xác minh", profile_location: "isolated-ui-fixture",
        open: true, busy: false, diagnostics: { captcha_visible: true, is_authenticated: false },
      } });
    });
    await page.goto(`/?job=${job.id}&mode=${platform}&q=decor`);
    await expect(page.getByRole("button", { name: "Đăng nhập bằng mã QR", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Xem trang xác minh", exact: true }).click();
    await expect(page.getByRole("img", { name: /Trang xác minh/ })).toBeVisible();
    await page.getByRole("button", { name: "Tải lại CAPTCHA không hiển thị" }).click();
    await expect.poll(() => refreshCount).toBe(1);
    await expect(page.getByRole("button", { name: "Tiếp tục lượt tìm", exact: true })).toBeVisible();
  });
}
