import { test, expect } from "@playwright/test";

test.describe("System Health & Release Readiness UI", () => {
  test("renders health page, release gate checks, and scale telemetry without errors", async ({
    page,
  }) => {
    // Collect any page console errors or uncaught exceptions
    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(err.message));

    await page.goto("/health");

    // 1. Check main heading
    await expect(
      page.getByRole("heading", {
        name: "Giám sát hệ thống & Chất lượng (Release Gate)",
      })
    ).toBeVisible({ timeout: 10000 });

    // 2. Check overview cards
    await expect(page.getByText("Trạng thái Backend", { exact: true })).toBeVisible();
    await expect(page.getByText("Release Readiness", { exact: true })).toBeVisible();
    await expect(page.getByText("Hàng đợi (Queue Size)", { exact: true })).toBeVisible();
    await expect(page.getByText("Worker Pool", { exact: true })).toBeVisible();
    await expect(page.getByText("Uptime", { exact: true })).toBeVisible();

    // 3. Check Scale Infrastructure & Queue section
    await expect(
      page.getByText("Hạ tầng mở rộng (Phase 10: Scale Infrastructure)")
    ).toBeVisible();

    // 4. Check Observability & Telemetry section
    await expect(
      page.getByText("Giám sát độ trễ & Lỗi (Phase 11: Telemetry)")
    ).toBeVisible();

    // 5. Check Release Gate Checks section
    await expect(
      page.getByText("Tiêu chuẩn nghiệm thu phát hành (Release Gate Checks)")
    ).toBeVisible();

    // 6. Check Multi-Platform Capabilities Matrix
    await expect(
      page.getByText("Ma trận năng lực đa nền tảng (Phase 9: Multi-Platform Architecture)")
    ).toBeVisible();

    // 7. Test refresh button action
    const refreshBtn = page.getByRole("button", { name: "Làm mới" });
    await expect(refreshBtn).toBeVisible();
    await refreshBtn.click();

    // Ensure no JavaScript exceptions occurred (e.g. .map() on object)
    expect(pageErrors).toEqual([]);
  });
});
