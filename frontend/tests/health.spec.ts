import { test, expect } from "@playwright/test";

test.describe("System Health & Release Readiness UI", () => {
  test("waits for readiness response and renders check cards and scale telemetry", async ({
    page,
  }) => {
    // Collect any page console errors or uncaught exceptions
    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(err.message));

    // Wait for the release-readiness API response
    const readinessPromise = page.waitForResponse(
      (resp) =>
        resp.url().includes("/system/release-readiness") &&
        resp.status() === 200
    );

    await page.goto("/health");
    await readinessPromise;

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

    // 5. Verify actual readiness check cards and their statuses from backend API
    const dbCheck = page.locator("[data-testid='readiness-check-database']");
    await expect(dbCheck).toBeVisible({ timeout: 10000 });
    await expect(dbCheck).toContainText("database (PASS)");

    const workerCheck = page.locator("[data-testid='readiness-check-worker_pool']");
    await expect(workerCheck).toBeVisible();

    const providerCheck = page.locator("[data-testid='readiness-check-providers']");
    await expect(providerCheck).toBeVisible();

    // 6. Check Multi-Platform Capabilities Matrix
    await expect(
      page.getByText("Ma trận năng lực đa nền tảng (Phase 9: Multi-Platform Architecture)")
    ).toBeVisible();

    // 7. Test refresh button action and verify response
    const refreshBtn = page.getByRole("button", { name: "Làm mới" });
    await expect(refreshBtn).toBeVisible();
    const refreshReadinessPromise = page.waitForResponse(
      (resp) =>
        resp.url().includes("/system/release-readiness") &&
        resp.status() === 200
    );
    await refreshBtn.click();
    await refreshReadinessPromise;

    // Ensure database check is still visible and passed
    await expect(dbCheck).toBeVisible();

    // Ensure no JavaScript exceptions occurred
    expect(pageErrors).toEqual([]);
  });

  test("triggers live probe and renders live network reachability breakdown", async ({
    page,
  }) => {
    // Collect any page console errors or uncaught exceptions
    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(err.message));

    await page.goto("/health");

    // Intercept the live probe call to verify UI response
    await page.route("**/api/system/release-readiness?probe=true", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "DEGRADED",
          is_ready: false,
          checks: {
            database: { status: "PASS", detail: "All 5 core tables verified" },
            worker_pool: { status: "PASS", detail: "Worker pool healthy (2 workers)" },
            providers: { status: "PASS", detail: "Provider states operational" },
            live_providers: {
              status: "PASS",
              detail: "All providers live verified: {'douyin': {'reachable': True, 'state': 'HTTP 200: verification_required'}, 'xiaohongshu': {'reachable': True, 'state': 'HTTP 200: login_required'}}",
            },
          },
          warnings: ["Provider auth challenge active: douyin=HTTP 200: verification_required, xhs=HTTP 200: login_required"],
          live_probe: {
            douyin: {
              reachable: true,
              state: "HTTP 200: verification_required",
            },
            xiaohongshu: {
              reachable: true,
              state: "HTTP 200: login_required",
            },
          },
        }),
      });
    });

    const liveProbeBtn = page.locator("[data-testid='run-live-probe-btn']");
    await expect(liveProbeBtn).toBeVisible();
    await liveProbeBtn.click();

    // Verify live probe active badge and breakdown panel appear
    await expect(page.locator("[data-testid='live-probe-active-badge']")).toBeVisible({ timeout: 10000 });
    await expect(page.locator("[data-testid='live-probe-breakdown']")).toBeVisible();

    // Verify distinct Douyin status: Reachable but Auth Challenge Required
    const douyinCard = page.locator("[data-testid='live-probe-douyin']");
    await expect(douyinCard).toBeVisible();
    await expect(douyinCard).toContainText("Đã kết nối: HTTP 200: verification_required");
    await expect(page.locator("[data-testid='live-probe-douyin-badge']")).toContainText("CẦN XÁC MINH BẢO MẬT");

    // Verify distinct Xiaohongshu status: Reachable but Login Required
    const xhsCard = page.locator("[data-testid='live-probe-xiaohongshu']");
    await expect(xhsCard).toBeVisible();
    await expect(xhsCard).toContainText("Đã kết nối: HTTP 200: login_required");
    await expect(page.locator("[data-testid='live-probe-xiaohongshu-badge']")).toContainText("CẦN ĐĂNG NHẬP PHIÊN");

    // Verify live_providers check card appears in the checks grid
    const liveProvidersCheck = page.locator("[data-testid='readiness-check-live_providers']");
    await expect(liveProvidersCheck).toBeVisible();
    await expect(liveProvidersCheck).toContainText("live providers (PASS)");

    // Ensure no JavaScript exceptions occurred
    expect(pageErrors).toEqual([]);
  });
});
