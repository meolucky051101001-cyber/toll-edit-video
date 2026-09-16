import { test, expect } from "@playwright/test";

test.describe("Video Download Controls & UX", () => {
  test("mock videos display disabled download button with tooltip", async ({
    page,
  }) => {
    await page.goto("/");
    await page
      .getByRole("checkbox", { name: "Tự tạo từ khóa AI khi tìm" })
      .uncheck();

    // Select Mock mode
    await page
      .getByRole("combobox", { name: "Nguồn tìm kiếm" })
      .selectOption("mock");

    await page
      .getByRole("textbox", { name: "Chủ đề tìm kiếm" })
      .fill("test mock download");
    await page
      .getByRole("combobox", { name: "Số kết quả", exact: true })
      .selectOption("20");
    await page
      .getByRole("button", { name: "Tìm video mẫu", exact: true })
      .click();

    await expect(page.getByText("Hoàn tất", { exact: true })).toBeVisible({
      timeout: 10000,
    });

    const cards = page.getByTestId("video-card");
    await expect(cards).toHaveCount(20);

    const firstCard = cards.first();
    const downloadArea = firstCard.getByTestId("download-controls");
    await expect(downloadArea).toBeVisible();

    const dlBtn = downloadArea.getByRole("button", {
      name: /Tải video \(mẫu không khả dụng\)/,
    });
    await expect(dlBtn).toBeVisible();
    await expect(dlBtn).toBeDisabled();
    await expect(dlBtn).toHaveAttribute(
      "title",
      "Video mẫu không hỗ trợ tải về"
    );
  });

  test("real video card shows active download progress and completed save link", async ({
    page,
  }) => {
    const jobData = {
      id: "job-test-dl-1",
      original_query: "test real dl",
      platforms: ["xiaohongshu"],
      status: "completed",
      requested_limit: 1,
      found_count: 1,
      processed_count: 1,
      duplicate_count: 0,
      provider_counts: { xiaohongshu: 1 },
      created_at: new Date().toISOString(),
      error_message: null,
      is_mock: false,
      queries: ["test real dl"],
      ai_source: null,
      ai_warning: null,
      query_expansion: null,
    };

    // Intercept search API to return a simulated real video
    await page.route(/\/api\/search(\?.*)?$/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(jobData),
      });
    });

    await page.route(/\/api\/search\/jobs\/.*/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(jobData),
      });
    });

    await page.route(/\/api\/videos/, async (route) => {
      const url = route.request().url();
      if (url.includes("/downloads/active")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "null",
        });
        return;
      }
      if (url.includes("/downloads") && route.request().method() === "POST") {
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({
            id: "dl-job-e2e-1",
            video_id: "vid-e2e-1",
            platform: "xiaohongshu",
            status: "downloading",
            progress_percent: 45.5,
            bytes_downloaded: 4771020,
            total_bytes: 10485760,
            error_code: null,
            error_message: null,
            download_url: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
        });
        return;
      }

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: "vid-e2e-1",
              platform: "xiaohongshu",
              url: "https://www.xiaohongshu.com/explore/66d3cb2b00000000200399aa",
              share_url:
                "https://www.xiaohongshu.com/explore/66d3cb2b00000000200399aa?xsec_token=CB12345",
              title: "Test Real Video Title",
              caption: "Test caption",
              thumbnail_url: null,
              author_name: "Test Author",
              hashtags: ["test"],
              keywords: ["test"],
              like_count: 100,
              comment_count: 10,
              favorite_count: 5,
              share_count: 2,
              duration_seconds: 30,
              published_at: new Date().toISOString(),
              relevance_score: 95,
              quality_score: 90,
              final_score: 92,
              status: "new",
              is_mock: false,
              search_query: "test real dl",
              search_job_id: "job-test-dl-1",
            },
          ],
          total: 1,
          page: 1,
          page_size: 20,
        }),
      });
    });

    let pollCount = 0;
    await page.route(/\/api\/downloads\/.*/, async (route) => {
      pollCount++;
      if (pollCount <= 1) {
        // First poll: still downloading
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: "dl-job-e2e-1",
            video_id: "vid-e2e-1",
            platform: "xiaohongshu",
            status: "downloading",
            progress_percent: 75.0,
            bytes_downloaded: 7864320,
            total_bytes: 10485760,
            error_code: null,
            error_message: null,
            download_url: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
        });
      } else {
        // Second poll: completed
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: "dl-job-e2e-1",
            video_id: "vid-e2e-1",
            platform: "xiaohongshu",
            status: "completed",
            progress_percent: 100.0,
            bytes_downloaded: 10485760,
            total_bytes: 10485760,
            error_code: null,
            error_message: null,
            download_url: "/api/downloads/dl-job-e2e-1/file",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
        });
      }
    });

    await page.goto("/");
    await page
      .getByRole("checkbox", { name: "Tự tạo từ khóa AI khi tìm" })
      .uncheck();

    // Trigger search
    await page
      .getByRole("textbox", { name: "Chủ đề tìm kiếm" })
      .fill("test real dl");
    await page.locator('button[type="submit"]').click();

    // Verify card rendered
    const card = page.getByTestId("video-card").first();
    await expect(card).toBeVisible({ timeout: 10000 });
    await expect(card.locator(".card-title")).toContainText(
      "Test Real Video Title"
    );

    // Initial state: Idle button "Tải video"
    const dlControls = card.getByTestId("download-controls");
    const startBtn = dlControls.getByRole("button", {
      name: "Tải video",
      exact: true,
    });
    await expect(startBtn).toBeVisible();

    // Click "Tải video"
    await startBtn.click();

    // Should enter downloading state
    await expect(dlControls.locator(".download-progress-track")).toBeVisible();
    await expect(dlControls.locator(".download-info-row")).toContainText(
      "MB / 10.0 MB"
    );

    // After polling completes, should transition to "Lưu file về máy"
    const saveLink = dlControls.getByRole("link", {
      name: /Lưu file về máy/,
    });
    await expect(saveLink).toBeVisible({ timeout: 5000 });
    await expect(saveLink).toHaveAttribute(
      "href",
      "/api/downloads/dl-job-e2e-1/file"
    );
    await expect(saveLink).toHaveAttribute("download", "");
  });

  test("download error shows friendly message and retry button", async ({
    page,
  }) => {
    const jobData = {
      id: "job-test-dl-2",
      original_query: "test err",
      platforms: ["douyin"],
      status: "completed",
      requested_limit: 1,
      found_count: 1,
      processed_count: 1,
      duplicate_count: 0,
      provider_counts: { douyin: 1 },
      created_at: new Date().toISOString(),
      error_message: null,
      is_mock: false,
      queries: ["test err"],
      ai_source: null,
      ai_warning: null,
      query_expansion: null,
    };

    await page.route(/\/api\/search(\?.*)?$/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(jobData),
      });
    });

    await page.route(/\/api\/search\/jobs\/.*/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(jobData),
      });
    });

    await page.route(/\/api\/videos/, async (route) => {
      const url = route.request().url();
      if (url.includes("/downloads/active")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "null",
        });
        return;
      }
      if (url.includes("/downloads") && route.request().method() === "POST") {
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({
            id: "dl-job-err-1",
            video_id: "vid-err-1",
            platform: "douyin",
            status: "failed",
            progress_percent: null,
            bytes_downloaded: 0,
            total_bytes: null,
            error_code: "upstream_offline",
            error_message:
              "Không kết nối được dịch vụ Douyin-Downloader (127.0.0.1:5555). Hãy khởi động dịch vụ và thử lại.",
            download_url: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
        });
        return;
      }

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: "vid-err-1",
              platform: "douyin",
              url: "https://www.douyin.com/video/7400000000000000000",
              title: "Douyin Error Video",
              caption: "",
              thumbnail_url: null,
              author_name: "Douyin Creator",
              hashtags: [],
              keywords: [],
              like_count: 10,
              comment_count: 1,
              favorite_count: 0,
              share_count: 0,
              duration_seconds: 15,
              published_at: new Date().toISOString(),
              relevance_score: 90,
              quality_score: 90,
              final_score: 90,
              status: "new",
              is_mock: false,
              search_query: "test err",
              search_job_id: "job-test-dl-2",
            },
          ],
          total: 1,
          page: 1,
          page_size: 20,
        }),
      });
    });

    await page.goto("/");
    await page
      .getByRole("checkbox", { name: "Tự tạo từ khóa AI khi tìm" })
      .uncheck();

    // Select Douyin mode
    await page
      .getByRole("combobox", { name: "Nguồn tìm kiếm" })
      .selectOption("douyin");

    await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("test err");
    await page.locator('button[type="submit"]').click();

    const card = page.getByTestId("video-card").first();
    await expect(card).toBeVisible({ timeout: 10000 });

    const dlControls = card.getByTestId("download-controls");
    await dlControls.getByRole("button", { name: "Tải video", exact: true }).click();

    // Verify error box and message
    await expect(dlControls.locator(".download-failed-box")).toBeVisible();
    await expect(dlControls.locator(".download-error-text")).toContainText(
      "Không kết nối được dịch vụ Douyin-Downloader"
    );

    // Verify retry button is visible
    const retryBtn = dlControls.getByRole("button", { name: "Thử lại", exact: true });
    await expect(retryBtn).toBeVisible();
  });

  test("indeterminate progress displays correctly when total_bytes is unknown", async ({
    page,
  }) => {
    const jobData = {
      id: "job-test-dl-3",
      original_query: "test indeterminate",
      platforms: ["xiaohongshu"],
      status: "completed",
      requested_limit: 1,
      found_count: 1,
      processed_count: 1,
      duplicate_count: 0,
      provider_counts: { xiaohongshu: 1 },
      created_at: new Date().toISOString(),
      error_message: null,
      is_mock: false,
      queries: ["test indeterminate"],
      ai_source: null,
      ai_warning: null,
      query_expansion: null,
    };

    await page.route(/\/api\/search(\?.*)?$/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(jobData),
      });
    });

    await page.route(/\/api\/search\/jobs\/.*/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(jobData),
      });
    });

    await page.route(/\/api\/videos/, async (route) => {
      const url = route.request().url();
      if (url.includes("/downloads/active")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "null",
        });
        return;
      }
      if (url.includes("/downloads") && route.request().method() === "POST") {
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({
            id: "dl-job-indet-1",
            video_id: "vid-indet-1",
            platform: "xiaohongshu",
            status: "downloading",
            progress_percent: null,
            bytes_downloaded: 4194304, // 4.0 MB
            total_bytes: null,
            error_code: null,
            error_message: null,
            download_url: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
        });
        return;
      }

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: "vid-indet-1",
              platform: "xiaohongshu",
              url: "https://www.xiaohongshu.com/explore/66d3cb2b00000000200399cc",
              title: "Indeterminate Test Video",
              caption: "",
              thumbnail_url: null,
              author_name: "XHS Author",
              hashtags: [],
              keywords: [],
              like_count: 50,
              comment_count: 5,
              favorite_count: 2,
              share_count: 1,
              duration_seconds: 20,
              published_at: new Date().toISOString(),
              relevance_score: 90,
              quality_score: 90,
              final_score: 90,
              status: "new",
              is_mock: false,
              search_query: "test indeterminate",
              search_job_id: "job-test-dl-3",
            },
          ],
          total: 1,
          page: 1,
          page_size: 20,
        }),
      });
    });

    await page.goto("/");
    await page
      .getByRole("checkbox", { name: "Tự tạo từ khóa AI khi tìm" })
      .uncheck();

    await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("test indeterminate");
    await page.locator('button[type="submit"]').click();

    const card = page.getByTestId("video-card").first();
    await expect(card).toBeVisible({ timeout: 10000 });

    const dlControls = card.getByTestId("download-controls");
    await dlControls.getByRole("button", { name: "Tải video", exact: true }).click();

    // Verify indeterminate progress track is visible with animate-pulse
    await expect(dlControls.locator(".download-progress-track")).toBeVisible();
    await expect(dlControls.locator(".download-progress-indeterminate")).toBeVisible();
    // Verify bytes downloaded text displays "4.0 MB đã tải" without percentage
    await expect(dlControls.locator(".download-bytes-text")).toContainText("4.0 MB đã tải");
  });
});
