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
    const readinessStatus = page.locator("[data-testid='release-readiness-status']");
    await expect(readinessStatus).toBeVisible();
    // Default without live probe has warnings for standby sessions -> DEGRADED (amber rgb(202, 138, 4), not green rgb(22, 163, 74))
    await expect(readinessStatus).toHaveText("DEGRADED");
    await expect(readinessStatus).toHaveCSS("color", "rgb(202, 138, 4)");
    await expect(readinessStatus).not.toHaveCSS("color", "rgb(22, 163, 74)");
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

  test("triggers live probe, verifies session distinction, and clears stale live data on refresh", async ({
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
          workstation_ready: true,
          retrieval_ready: false,
          checks: {
            database: { status: "PASS", detail: "All 5 core tables verified" },
            worker_pool: { status: "PASS", detail: "Worker pool healthy (2 workers)" },
            providers: { status: "PASS", detail: "Provider browser services verified live" },
            live_providers: {
              status: "PASS",
              detail: "All providers live verified: {'douyin': {'reachable': True, 'state': 'HTTP 200: verification_required'}, 'xiaohongshu': {'reachable': True, 'state': 'HTTP 200: page_available'}}",
            },
          },
          warnings: ["Provider auth challenge / unverified session: douyin=HTTP 200: verification_required, xhs=HTTP 200: page_available"],
          live_probe: {
            douyin: {
              reachable: true,
              state: "HTTP 200: verification_required",
            },
            xiaohongshu: {
              reachable: true,
              state: "HTTP 200: page_available",
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

    // Overview readiness status should be DEGRADED (amber rgb(202, 138, 4), not green rgb(22, 163, 74))
    const liveReadinessStatus = page.locator("[data-testid='release-readiness-status']");
    await expect(liveReadinessStatus).toHaveText("DEGRADED");
    await expect(liveReadinessStatus).toHaveCSS("color", "rgb(202, 138, 4)");
    await expect(liveReadinessStatus).not.toHaveCSS("color", "rgb(22, 163, 74)");

    // Verify distinct Douyin status: Reachable but Auth Challenge Required
    const douyinCard = page.locator("[data-testid='live-probe-douyin']");
    await expect(douyinCard).toBeVisible();
    await expect(douyinCard).toContainText("Kết nối mạng: Đã kết nối (HTTP 200)");
    await expect(douyinCard).toContainText("Phát hiện thử thách xác minh bảo mật");
    await expect(page.locator("[data-testid='live-probe-douyin-badge']")).toContainText("CẦN XÁC MINH BẢO MẬT");

    // Verify Xiaohongshu status: Reachable but Unverified Session (must NOT show false READY)
    const xhsCard = page.locator("[data-testid='live-probe-xiaohongshu']");
    await expect(xhsCard).toBeVisible();
    await expect(xhsCard).toContainText("Kết nối mạng: Đã kết nối (HTTP 200)");
    await expect(xhsCard).toContainText("Chưa xác minh phiên đăng nhập");
    const xhsBadge = page.locator("[data-testid='live-probe-xiaohongshu-badge']");
    await expect(xhsBadge).toContainText("CHƯA XÁC MINH PHIÊN");
    await expect(xhsBadge).not.toContainText("ĐÃ KẾT NỐI (READY)");

    // Verify live_providers check card appears in the checks grid
    const liveProvidersCheck = page.locator("[data-testid='readiness-check-live_providers']");
    await expect(liveProvidersCheck).toBeVisible();
    await expect(liveProvidersCheck).toContainText("live providers (PASS)");

    // Test clearing live probe results when user clicks "Làm mới"
    const refreshBtn = page.getByRole("button", { name: "Làm mới" });
    await refreshBtn.click();

    // Verify live probe active badge and breakdown panel are cleared from DOM
    await expect(page.locator("[data-testid='live-probe-active-badge']")).not.toBeVisible();
    await expect(page.locator("[data-testid='live-probe-breakdown']")).not.toBeVisible();

    // Ensure no JavaScript exceptions occurred
    expect(pageErrors).toEqual([]);
  });

  test("displays XHS verification_required (CAPTCHA) with amber badge and resolution tips", async ({
    page,
  }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(err.message));

    await page.goto("/health");

    await page.route("**/api/system/release-readiness?probe=true", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "DEGRADED",
          is_ready: false,
          workstation_ready: true,
          retrieval_ready: false,
          checks: {
            database: { status: "PASS", detail: "All 5 core tables verified" },
            worker_pool: { status: "PASS", detail: "Worker pool healthy (2 workers)" },
            providers: { status: "PASS", detail: "Provider browser services verified live" },
            live_providers: {
              status: "PASS",
              detail: "All providers live verified",
            },
          },
          warnings: ["Provider auth challenge / unverified session: xhs=HTTP 200: verification_required"],
          live_probe: {
            douyin: {
              reachable: true,
              http_status: 200,
              session_state: "authenticated",
              state: "HTTP 200: authenticated",
            },
            xiaohongshu: {
              reachable: true,
              http_status: 200,
              session_state: "verification_required",
              state: "HTTP 200: verification_required",
            },
          },
        }),
      });
    });

    const liveProbeBtn = page.locator("[data-testid='run-live-probe-btn']");
    await expect(liveProbeBtn).toBeVisible();
    await liveProbeBtn.click();

    await expect(page.locator("[data-testid='live-probe-active-badge']")).toBeVisible({ timeout: 10000 });
    const xhsBadge = page.locator("[data-testid='live-probe-xiaohongshu-badge']");
    await expect(xhsBadge).toBeVisible();
    await expect(xhsBadge).toHaveText("CẦN XÁC MINH BẢO MẬT (CAPTCHA)");
    await expect(xhsBadge).toHaveCSS("background-color", "rgb(254, 243, 199)"); // #fef3c7
    await expect(xhsBadge).toHaveCSS("color", "rgb(146, 64, 14)"); // #92400e

    const xhsCard = page.locator("[data-testid='live-probe-xiaohongshu']");
    await expect(xhsCard).toContainText("Phát hiện thử thách xác minh bảo mật (CAPTCHA - cần giải trên trạm làm việc)");
    await expect(xhsCard).toContainText("Cần mở trình duyệt hoặc giải CAPTCHA trạm làm việc để duyệt tìm kiếm");

    // Overview readiness status should still be DEGRADED (amber rgb(202, 138, 4), not green)
    const readinessStatus = page.locator("[data-testid='release-readiness-status']");
    await expect(readinessStatus).toHaveText("DEGRADED");
    await expect(readinessStatus).toHaveCSS("color", "rgb(202, 138, 4)");
    await expect(readinessStatus).not.toHaveCSS("color", "rgb(22, 163, 74)");

    expect(pageErrors).toEqual([]);
  });

  test("displays APPROVED with green styling when both platforms have authenticated sessions", async ({
    page,
  }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(err.message));

    await page.goto("/health");

    await page.route("**/api/system/release-readiness?probe=true", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "READY",
          is_ready: true,
          workstation_ready: true,
          retrieval_ready: true,
          checks: {
            database: { status: "PASS", detail: "All 5 core tables verified" },
            worker_pool: { status: "PASS", detail: "Worker pool healthy (2 workers)" },
            providers: { status: "PASS", detail: "Provider browser services verified live" },
            live_providers: {
              status: "PASS",
              detail: "All providers live verified",
            },
          },
          warnings: [],
          live_probe: {
            douyin: {
              reachable: true,
              http_status: 200,
              session_state: "authenticated",
              state: "HTTP 200: authenticated",
            },
            xiaohongshu: {
              reachable: true,
              http_status: 200,
              session_state: "authenticated",
              state: "HTTP 200: authenticated",
            },
          },
        }),
      });
    });

    const liveProbeBtn = page.locator("[data-testid='run-live-probe-btn']");
    await expect(liveProbeBtn).toBeVisible();
    await liveProbeBtn.click();

    await expect(page.locator("[data-testid='live-probe-active-badge']")).toBeVisible({ timeout: 10000 });

    const readinessStatus = page.locator("[data-testid='release-readiness-status']");
    await expect(readinessStatus).toHaveText("APPROVED");
    await expect(readinessStatus).toHaveCSS("color", "rgb(22, 163, 74)"); // #16a34a

    const dyBadge = page.locator("[data-testid='live-probe-douyin-badge']");
    await expect(dyBadge).toHaveText("ĐÃ ĐĂNG NHẬP (SẴN SÀNG)");
    await expect(dyBadge).toHaveCSS("background-color", "rgb(220, 252, 231)"); // #dcfce7
    await expect(dyBadge).toHaveCSS("color", "rgb(22, 101, 52)"); // #166534

    const xhsBadge = page.locator("[data-testid='live-probe-xiaohongshu-badge']");
    await expect(xhsBadge).toHaveText("ĐÃ ĐĂNG NHẬP (SẴN SÀNG)");
    await expect(xhsBadge).toHaveCSS("background-color", "rgb(220, 252, 231)"); // #dcfce7
    await expect(xhsBadge).toHaveCSS("color", "rgb(22, 101, 52)"); // #166534

    const xhsCard = page.locator("[data-testid='live-probe-xiaohongshu']");
    await expect(xhsCard).toContainText("Đã xác thực phiên thành công (Sẵn sàng tìm kiếm)");

    expect(pageErrors).toEqual([]);
  });
});
