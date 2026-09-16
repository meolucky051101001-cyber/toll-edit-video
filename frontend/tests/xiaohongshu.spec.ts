import { test, expect } from "@playwright/test";

interface SearchPayload {
  mode?: string;
  platforms?: string[];
  limit?: number;
  use_ai?: boolean;
  query?: string;
  selected_queries?: string[];
}

test("real mode submits XHS only and displays manual resume controls", async ({ page }) => {
  let state = "waiting_for_login";
  const job = () => ({
    id: "xhs-ui-fixture",
    original_query: "文具",
    platforms: ["xiaohongshu"],
    status: state,
    requested_limit: 5,
    found_count: 0,
    processed_count: 0,
    duplicate_count: 0,
    provider_counts: {},
    created_at: new Date().toISOString(),
    error_message: state === "waiting_for_login" ? "Hãy đăng nhập thủ công" : null,
    is_mock: false,
    queries: ["文具"],
    ai_warning: null,
  });
  await page.route("**/api/browser/xiaohongshu**", (route) =>
    route.fulfill({
      json: {
        state: "login_required",
        message: "Đăng nhập trong trình duyệt riêng",
        profile_location: "test-profile",
        open: true,
        busy: false,
      },
    }),
  );
  await page.route("**/api/search", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.mode).toBe("xiaohongshu");
    expect(body.platforms).toEqual(["xiaohongshu"]);
    expect(body.limit).toBe(5);
    expect(body.use_ai).toBe(false);
    await route.fulfill({ status: 202, json: job() });
  });
  await page.route("**/api/search/jobs/xhs-ui-fixture**", async (route) => {
    if (route.request().url().endsWith("/resume")) state = "completed";
    await route.fulfill({ json: job() });
  });
  await page.route("**/api/videos?**", (route) =>
    route.fulfill({ json: { items: [], total: 0, page: 1 } }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("checkbox", { name: "Tự tạo từ khóa AI khi tìm" }),
  ).not.toBeChecked();
  await page.getByRole("combobox", { name: "Nguồn tìm kiếm" }).selectOption("xiaohongshu");
  await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("文具");
  await page.getByRole("combobox", { name: "Số kết quả", exact: true }).selectOption("5");
  await page.getByRole("button", { name: /Tìm trên Xiaohongshu/ }).click();
  await expect(page.getByText("Chờ đăng nhập", { exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Nguồn tìm kiếm" })).toHaveValue("xiaohongshu");
  await page.getByRole("button", { name: "Tiếp tục lượt tìm", exact: true }).click();
  await expect(page.getByText("Hoàn tất", { exact: true })).toBeVisible();
});

test("default AI toggle is false on initial load and submits use_ai: false without clicking checkbox", async ({
  page,
}) => {
  const searchReqPromise = page.waitForRequest(
    (req) => req.url().includes("/api/search") && req.method() === "POST",
  );
  await page.route("**/api/search", async (route) => {
    await route.fulfill({
      status: 202,
      json: {
        id: "ai-check-fixture",
        original_query: "test",
        platforms: ["douyin", "xiaohongshu"],
        status: "completed",
        requested_limit: 20,
        found_count: 0,
        processed_count: 0,
        duplicate_count: 0,
        provider_counts: {},
        created_at: new Date().toISOString(),
        error_message: null,
        is_mock: true,
        queries: ["test"],
        ai_warning: null,
      },
    });
  });
  await page.route("**/api/search/jobs/**", (route) =>
    route.fulfill({
      json: { id: "ai-check-fixture", status: "completed", queries: ["test"] },
    }),
  );
  await page.route("**/api/videos?**", (route) =>
    route.fulfill({ json: { items: [], total: 0, page: 1 } }),
  );

  await page.goto("/");
  const aiCheckbox = page.getByRole("checkbox", {
    name: "Tự tạo từ khóa AI khi tìm",
  });
  await expect(aiCheckbox).not.toBeChecked();

  await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("unbox đồ cute");
  await page.getByRole("button", { name: "Tìm video mẫu" }).click();

  const searchReq = await searchReqPromise;
  const payload = searchReq.postDataJSON() as SearchPayload;
  expect(payload.use_ai).toBe(false);
});

test("mode switch: mock 200 -> XHS normalizes limit to 20 in UI and payload", async ({ page }) => {
  const searchReqPromise = page.waitForRequest(
    (req) => req.url().includes("/api/search") && req.method() === "POST",
  );
  await page.route("**/api/search", async (route) => {
    await route.fulfill({
      status: 202,
      json: {
        id: "xhs-mode-fixture",
        original_query: "test",
        platforms: ["xiaohongshu"],
        status: "completed",
        requested_limit: 20,
        found_count: 0,
        processed_count: 0,
        duplicate_count: 0,
        provider_counts: { xiaohongshu: 0 },
        created_at: new Date().toISOString(),
        error_message: null,
        is_mock: false,
        queries: ["test"],
        ai_warning: null,
      },
    });
  });
  await page.route("**/api/search/jobs/**", (route) =>
    route.fulfill({
      json: {
        id: "xhs-mode-fixture",
        status: "completed",
        queries: ["test"],
        provider_counts: { xiaohongshu: 0 },
        requested_limit: 20,
        processed_count: 0,
        found_count: 0,
        duplicate_count: 0,
      },
    }),
  );
  await page.route("**/api/videos?**", (route) =>
    route.fulfill({ json: { items: [], total: 0, page: 1 } }),
  );

  await page.goto("/");
  const limitSelect = page.getByRole("combobox", { name: "Số kết quả", exact: true });
  await limitSelect.selectOption("200");
  await expect(limitSelect).toHaveValue("200");

  const modeSelect = page.getByRole("combobox", { name: "Nguồn tìm kiếm" });
  await modeSelect.selectOption("xiaohongshu");

  // Visual/DOM check: dropdown normalized to 20
  await expect(limitSelect).toHaveValue("20");

  // Submit and check payload
  await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("文具");
  await page.getByRole("button", { name: /Tìm trên Xiaohongshu/ }).click();

  const searchReq = await searchReqPromise;
  const payload = searchReq.postDataJSON() as SearchPayload;
  expect(payload.mode).toBe("xiaohongshu");
  expect(payload.limit).toBe(20);
});

test("mode switch: mock 200 -> Douyin normalizes limit to 20 in UI and payload", async ({ page }) => {
  const searchReqPromise = page.waitForRequest(
    (req) => req.url().includes("/api/search") && req.method() === "POST",
  );
  await page.route("**/api/search", async (route) => {
    await route.fulfill({
      status: 202,
      json: {
        id: "douyin-mode-fixture",
        original_query: "test",
        platforms: ["douyin"],
        status: "completed",
        requested_limit: 20,
        found_count: 0,
        processed_count: 0,
        duplicate_count: 0,
        provider_counts: { douyin: 0 },
        created_at: new Date().toISOString(),
        error_message: null,
        is_mock: false,
        queries: ["test"],
        ai_warning: null,
      },
    });
  });
  await page.route("**/api/search/jobs/**", (route) =>
    route.fulfill({
      json: {
        id: "douyin-mode-fixture",
        status: "completed",
        queries: ["test"],
        provider_counts: { douyin: 0 },
        requested_limit: 20,
        processed_count: 0,
        found_count: 0,
        duplicate_count: 0,
      },
    }),
  );
  await page.route("**/api/videos?**", (route) =>
    route.fulfill({ json: { items: [], total: 0, page: 1 } }),
  );

  await page.goto("/");
  const limitSelect = page.getByRole("combobox", { name: "Số kết quả", exact: true });
  await limitSelect.selectOption("200");
  await expect(limitSelect).toHaveValue("200");

  const modeSelect = page.getByRole("combobox", { name: "Nguồn tìm kiếm" });
  await modeSelect.selectOption("douyin");

  // Visual/DOM check: dropdown normalized to 20
  await expect(limitSelect).toHaveValue("20");

  // Submit and check payload
  await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("文具");
  await page.getByRole("button", { name: /Tìm trên Douyin/ }).click();

  const searchReq = await searchReqPromise;
  const payload = searchReq.postDataJSON() as SearchPayload;
  expect(payload.mode).toBe("douyin");
  expect(payload.limit).toBe(20);
});

test("mode switch: real 5 -> mock normalizes limit to 20 in UI and payload", async ({ page }) => {
  const searchReqPromise = page.waitForRequest(
    (req) => req.url().includes("/api/search") && req.method() === "POST",
  );
  await page.route("**/api/search", async (route) => {
    await route.fulfill({
      status: 202,
      json: {
        id: "mock-norm-fixture",
        original_query: "test",
        platforms: ["douyin", "xiaohongshu"],
        status: "completed",
        requested_limit: 20,
        found_count: 0,
        processed_count: 0,
        duplicate_count: 0,
        provider_counts: { douyin: 0, xiaohongshu: 0 },
        created_at: new Date().toISOString(),
        error_message: null,
        is_mock: true,
        queries: ["test"],
        ai_warning: null,
      },
    });
  });
  await page.route("**/api/search/jobs/**", (route) =>
    route.fulfill({
      json: {
        id: "mock-norm-fixture",
        status: "completed",
        queries: ["test"],
        provider_counts: { douyin: 0, xiaohongshu: 0 },
        requested_limit: 20,
        processed_count: 0,
        found_count: 0,
        duplicate_count: 0,
      },
    }),
  );
  await page.route("**/api/videos?**", (route) =>
    route.fulfill({ json: { items: [], total: 0, page: 1 } }),
  );

  await page.goto("/?mode=xiaohongshu");
  const modeSelect = page.getByRole("combobox", { name: "Nguồn tìm kiếm" });
  await expect(modeSelect).toHaveValue("xiaohongshu");

  const limitSelect = page.getByRole("combobox", { name: "Số kết quả", exact: true });
  await limitSelect.selectOption("5");
  await expect(limitSelect).toHaveValue("5");

  // Switch to mock: 5 is not allowed in mock [20, 50, 100, 200], normalizes to 20
  await modeSelect.selectOption("mock");
  await expect(limitSelect).toHaveValue("20");

  // Submit and check payload
  await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("unbox");
  await page.getByRole("button", { name: "Tìm video mẫu" }).click();

  const searchReq = await searchReqPromise;
  const payload = searchReq.postDataJSON() as SearchPayload;
  expect(payload.mode).toBe("mock");
  expect(payload.limit).toBe(20);
});


test("platform button switches between real platforms and normalizes limit if needed", async ({ page }) => {
  await page.goto("/?mode=xiaohongshu");
  const limitSelect = page.getByRole("combobox", { name: "Số kết quả", exact: true });
  await limitSelect.selectOption("5");
  await expect(limitSelect).toHaveValue("5");

  // Click platform button "Douyin" (in real mode, this switches to Douyin thật)
  await page.getByRole("button", { name: /Douyin/ }).click();
  const modeSelect = page.getByRole("combobox", { name: "Nguồn tìm kiếm" });
  await expect(modeSelect).toHaveValue("douyin");
  // 5 is valid in douyin, so it remains 5
  await expect(limitSelect).toHaveValue("5");
});

test("unvalidated mode query param defaults to mock in UI and search payload", async ({ page }) => {
  const searchReqPromise = page.waitForRequest(
    (req) => req.url().includes("/api/search") && req.method() === "POST",
  );
  await page.route("**/api/search", async (route) => {
    await route.fulfill({
      status: 202,
      json: {
        id: "bogus-mode-fixture",
        original_query: "unbox",
        platforms: ["douyin", "xiaohongshu"],
        status: "completed",
        requested_limit: 20,
        found_count: 0,
        processed_count: 0,
        duplicate_count: 0,
        provider_counts: {},
        created_at: new Date().toISOString(),
        error_message: null,
        is_mock: true,
        queries: ["unbox"],
        ai_warning: null,
      },
    });
  });
  await page.route("**/api/search/jobs/**", (route) =>
    route.fulfill({
      json: {
        id: "bogus-mode-fixture",
        status: "completed",
        queries: ["unbox"],
        provider_counts: {},
        requested_limit: 20,
        processed_count: 0,
        found_count: 0,
        duplicate_count: 0,
      },
    }),
  );
  await page.route("**/api/videos?**", (route) =>
    route.fulfill({ json: { items: [], total: 0, page: 1 } }),
  );

  await page.goto("/?mode=bogus");
  const modeSelect = page.getByRole("combobox", { name: "Nguồn tìm kiếm" });
  await expect(modeSelect).toHaveValue("mock");

  await page.getByRole("textbox", { name: "Chủ đề tìm kiếm" }).fill("unbox");
  await page.getByRole("button", { name: "Tìm video mẫu" }).click();

  const searchReq = await searchReqPromise;
  const payload = searchReq.postDataJSON() as SearchPayload;
  expect(payload.mode).toBe("mock");
  expect(payload.platforms).toEqual(["douyin", "xiaohongshu"]);
});

test("insecure url protocol (ftp://) is rejected with error banner and does not open window", async ({ page }) => {
  await page.route("**/api/videos?**", async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        page: 1,
        items: [
          {
            id: "xhs-ftp-1",
            platform: "xiaohongshu",
            url: "ftp://www.xiaohongshu.com/explore/64abc1234567890abcdef012",
            share_url: null,
            title: "Insecure FTP URL",
            caption: "Should be blocked",
            thumbnail_url: null,
            author_name: "Author FTP",
            hashtags: [],
            keywords: [],
            like_count: 10,
            comment_count: 0,
            favorite_count: 0,
            share_count: 0,
            duration_seconds: 10,
            published_at: null,
            is_mock: false,
            imported_from: null,
            quality_score: 50,
            relevance_score: 50,
            final_score: 50,
            status: "new",
          },
        ],
      },
    });
  });

  await page.goto("/library");

  await page.evaluate(() => {
    (window as unknown as { __openedUrls: string[] }).__openedUrls = [];
    window.open = (url: string | URL | undefined) => {
      (window as unknown as { __openedUrls: string[] }).__openedUrls.push(String(url));
      return null;
    };
  });

  const card = page.getByTestId("video-card").first();
  await card.locator(".thumbnail").click();

  const openedUrls = await page.evaluate(
    () => (window as unknown as { __openedUrls: string[] }).__openedUrls,
  );
  expect(openedUrls).toHaveLength(0);

  await expect(page.getByText(/Liên kết không thuộc nền tảng hợp lệ hoặc không an toàn/)).toBeVisible();
});

test("canonical Xiaohongshu URL without token opens window and displays non-blocking advisory; opening tokenized card clears advisory", async ({
  page,
}) => {
  const canonicalUrl = "https://www.xiaohongshu.com/explore/64abc1234567890abcdef012";
  const tokenUrl =
    "https://www.xiaohongshu.com/discovery/item/64def7890123456abcdef345?xsec_token=ValidToken789&xsec_source=pc_search";

  await page.route("**/api/videos?**", async (route) => {
    await route.fulfill({
      json: {
        total: 2,
        page: 1,
        items: [
          {
            id: "xhs-canon-1",
            platform: "xiaohongshu",
            url: canonicalUrl,
            share_url: null,
            title: "Canonical XHS Video Without Token",
            caption: "Description here",
            thumbnail_url: null,
            author_name: "Author 1",
            hashtags: ["tag1"],
            keywords: [],
            like_count: 120,
            comment_count: 10,
            favorite_count: 5,
            share_count: 0,
            duration_seconds: 15.5,
            published_at: null,
            is_mock: false,
            imported_from: "mediacrawler",
            quality_score: 80,
            relevance_score: 90,
            final_score: 85,
            status: "new",
          },
          {
            id: "xhs-token-2",
            platform: "xiaohongshu",
            url: "https://www.xiaohongshu.com/explore/64def7890123456abcdef345",
            share_url: tokenUrl,
            title: "Tokenized XHS Video",
            caption: "Description 2",
            thumbnail_url: null,
            author_name: "Author 2",
            hashtags: ["tag2"],
            keywords: [],
            like_count: 500,
            comment_count: 20,
            favorite_count: 15,
            share_count: 2,
            duration_seconds: 65.2,
            published_at: null,
            is_mock: false,
            imported_from: null,
            quality_score: 85,
            relevance_score: 95,
            final_score: 90,
            status: "new",
          },
        ],
      },
    });
  });

  await page.goto("/library");

  // Verify duration formatted to 0:16 (not 0:15.5)
  await expect(page.locator(".duration").first()).toHaveText("0:16");

  // Track window.open calls
  await page.evaluate(() => {
    (window as unknown as { __openedUrls: string[] }).__openedUrls = [];
    window.open = (url: string | URL | undefined) => {
      (window as unknown as { __openedUrls: string[] }).__openedUrls.push(String(url));
      return null;
    };
  });

  // Click thumbnail of the canonical card
  const cards = page.getByTestId("video-card");
  await cards.nth(0).locator(".thumbnail").click();

  // Assert window.open was called with the canonical URL
  let openedUrls = await page.evaluate(
    () => (window as unknown as { __openedUrls: string[] }).__openedUrls,
  );
  expect(openedUrls).toContain(canonicalUrl);

  // Assert non-blocking advisory notice appeared with role="status"
  const noticeBanner = page.getByRole("status");
  await expect(noticeBanner).toBeVisible();
  await expect(page.getByText(/Lưu ý: Liên kết mở bằng URL tiêu chuẩn/)).toBeVisible();

  // Click thumbnail of the tokenized card
  await cards.nth(1).locator(".thumbnail").click();

  openedUrls = await page.evaluate(
    () => (window as unknown as { __openedUrls: string[] }).__openedUrls,
  );
  expect(openedUrls).toContain(tokenUrl);

  // Assert notice was cleared after opening tokenized card
  await expect(noticeBanner).toHaveCount(0);

  // Re-open canonical card to show notice again, then test close button dismissal
  await cards.nth(0).locator(".thumbnail").click();
  await expect(page.getByRole("status")).toBeVisible();
  await page.getByRole("button", { name: "Đóng thông báo" }).click();
  await expect(page.getByRole("status")).toHaveCount(0);
});

test("stale advisory notice is cleared immediately when clicking card with invalid URL", async ({ page }) => {
  const canonicalUrl = "https://www.xiaohongshu.com/explore/64abc1234567890abcdef012";
  const tokenUrl =
    "https://www.xiaohongshu.com/discovery/item/64def7890123456abcdef345?xsec_token=ValidToken789&xsec_source=pc_search";
  const invalidUrl = "ftp://www.xiaohongshu.com/explore/64invalid000000000000000";

  await page.route("**/api/videos?**", async (route) => {
    await route.fulfill({
      json: {
        total: 3,
        page: 1,
        items: [
          {
            id: "xhs-canon-1",
            platform: "xiaohongshu",
            url: canonicalUrl,
            share_url: null,
            title: "Canonical Video",
            caption: "Desc",
            thumbnail_url: null,
            author_name: "Auth 1",
            hashtags: [],
            keywords: [],
            like_count: 10,
            comment_count: 0,
            favorite_count: 0,
            share_count: 0,
            duration_seconds: 10,
            published_at: null,
            is_mock: false,
            imported_from: null,
            quality_score: 50,
            relevance_score: 50,
            final_score: 50,
            status: "new",
          },
          {
            id: "xhs-invalid-2",
            platform: "xiaohongshu",
            url: invalidUrl,
            share_url: null,
            title: "Invalid URL Video",
            caption: "Desc",
            thumbnail_url: null,
            author_name: "Auth 2",
            hashtags: [],
            keywords: [],
            like_count: 10,
            comment_count: 0,
            favorite_count: 0,
            share_count: 0,
            duration_seconds: 10,
            published_at: null,
            is_mock: false,
            imported_from: null,
            quality_score: 50,
            relevance_score: 50,
            final_score: 50,
            status: "new",
          },
          {
            id: "xhs-token-3",
            platform: "xiaohongshu",
            url: "https://www.xiaohongshu.com/explore/64def7890123456abcdef345",
            share_url: tokenUrl,
            title: "Tokenized Video",
            caption: "Desc",
            thumbnail_url: null,
            author_name: "Auth 3",
            hashtags: [],
            keywords: [],
            like_count: 10,
            comment_count: 0,
            favorite_count: 0,
            share_count: 0,
            duration_seconds: 10,
            published_at: null,
            is_mock: false,
            imported_from: null,
            quality_score: 50,
            relevance_score: 50,
            final_score: 50,
            status: "new",
          },
        ],
      },
    });
  });

  await page.goto("/library");

  await page.evaluate(() => {
    (window as unknown as { __openedUrls: string[] }).__openedUrls = [];
    window.open = (url: string | URL | undefined) => {
      (window as unknown as { __openedUrls: string[] }).__openedUrls.push(String(url));
      return null;
    };
  });

  const cards = page.getByTestId("video-card");

  // Step 1: Open canonical card -> notice is shown
  await cards.nth(0).locator(".thumbnail").click();
  await expect(page.getByRole("status")).toBeVisible();

  // Step 2: Open invalid/ftp card -> notice is cleared, error banner is shown (no coexistence)
  await cards.nth(1).locator(".thumbnail").click();
  await expect(page.getByRole("status")).toHaveCount(0);
  await expect(page.getByText(/Liên kết không thuộc nền tảng hợp lệ hoặc không an toàn/)).toBeVisible();

  // Step 3: Open tokenized card -> window.open called with token, notice remains cleared
  await cards.nth(2).locator(".thumbnail").click();
  await expect(page.getByRole("status")).toHaveCount(0);
  const openedUrls = await page.evaluate(
    () => (window as unknown as { __openedUrls: string[] }).__openedUrls,
  );
  expect(openedUrls).toContain(tokenUrl);
});

test("fake or empty token parameters correctly trigger advisory notice", async ({ page }) => {
  const nid = "65d3cb2b00000000200399e3";
  const fakeTokenUrl = `https://www.rednote.com/discovery/item/${nid}?not_xsec_token=FAKE_PARAM`;
  const emptyTokenUrl = `https://www.rednote.com/discovery/item/${nid}?xsec_token=`;

  await page.route("**/api/videos*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        page: 1,
        page_size: 24,
        total: 2,
        items: [
          {
            id: "vid-fake-param",
            platform: "xiaohongshu",
            platform_video_id: nid,
            url: `https://www.xiaohongshu.com/explore/${nid}`,
            share_url: fakeTokenUrl,
            title: "Card with not_xsec_token",
            caption: "Desc",
            thumbnail_url: null,
            author_name: "Author 1",
            hashtags: [],
            keywords: [],
            like_count: 10,
            comment_count: 0,
            favorite_count: 0,
            share_count: 0,
            duration_seconds: 10,
            published_at: null,
            is_mock: false,
            imported_from: null,
            quality_score: 50,
            relevance_score: 50,
            final_score: 50,
            status: "new",
          },
          {
            id: "vid-empty-token",
            platform: "xiaohongshu",
            platform_video_id: nid,
            url: `https://www.xiaohongshu.com/explore/${nid}`,
            share_url: emptyTokenUrl,
            title: "Card with empty xsec_token",
            caption: "Desc",
            thumbnail_url: null,
            author_name: "Author 2",
            hashtags: [],
            keywords: [],
            like_count: 20,
            comment_count: 0,
            favorite_count: 0,
            share_count: 0,
            duration_seconds: 15,
            published_at: null,
            is_mock: false,
            imported_from: null,
            quality_score: 50,
            relevance_score: 50,
            final_score: 50,
            status: "new",
          },
        ],
      }),
    });
  });

  await page.goto("/library");

  await page.evaluate(() => {
    (window as unknown as { __openedUrls: string[] }).__openedUrls = [];
    window.open = (url: string | URL | undefined) => {
      (window as unknown as { __openedUrls: string[] }).__openedUrls.push(String(url));
      return null;
    };
  });

  const cards = page.getByTestId("video-card");

  // Card 1: not_xsec_token should NOT be recognized as token, advisory notice MUST be shown
  await cards.nth(0).locator(".thumbnail").click();
  await expect(page.getByRole("status")).toBeVisible();
  await expect(page.getByText(/Lưu ý: Liên kết mở bằng URL tiêu chuẩn \(thiếu xsec_token\)/)).toBeVisible();

  // Close advisory
  await page.getByRole("button", { name: "Đóng thông báo" }).click();
  await expect(page.getByRole("status")).toHaveCount(0);

  // Card 2: ?xsec_token= (empty value) should NOT be recognized as token, advisory notice MUST be shown
  await cards.nth(1).locator(".thumbnail").click();
  await expect(page.getByRole("status")).toBeVisible();
  await expect(page.getByText(/Lưu ý: Liên kết mở bằng URL tiêu chuẩn \(thiếu xsec_token\)/)).toBeVisible();
});


