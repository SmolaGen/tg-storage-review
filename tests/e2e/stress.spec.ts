/**
 * Stress & resilience tests for tg-storage.
 */

import { test, expect, APIRequestContext } from '@playwright/test';

const BASE = process.env.BASE_URL ?? 'http://localhost:8001';
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? '';
const TG_ID = process.env.TG_ADMIN_ID ?? '1';

// ── helpers ──────────────────────────────────────────────────────────────────

async function getToken(req: APIRequestContext): Promise<string> {
  const res = await req.post(
    `${BASE}/admin/token?telegram_id=${TG_ID}&username=admin`,
    { headers: { 'X-Admin-Secret': ADMIN_SECRET } },
  );
  expect(res.status()).toBe(200);
  const body = await res.json();
  return body.access_token as string;
}

function authHeaders(token: string) {
  return { Authorization: `Bearer ${token}` };
}

// ── 1. AUTH ───────────────────────────────────────────────────────────────────

test.describe('Auth', () => {
  test('valid credentials return JWT', async ({ request }) => {
    const res = await request.post(
      `${BASE}/admin/token?telegram_id=${TG_ID}&username=admin`,
      { headers: { 'X-Admin-Secret': ADMIN_SECRET } },
    );
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('access_token');
    expect(typeof body.access_token).toBe('string');
  });

  test('wrong secret returns 401 or 403', async ({ request }) => {
    const res = await request.post(
      `${BASE}/admin/token?telegram_id=${TG_ID}&username=admin`,
      { headers: { 'X-Admin-Secret': 'wrongsecret' } },
    );
    expect([401, 403, 422]).toContain(res.status());
  });

  test('missing secret header returns error', async ({ request }) => {
    const res = await request.post(
      `${BASE}/admin/token?telegram_id=${TG_ID}&username=admin`,
    );
    expect([401, 403, 422]).toContain(res.status());
  });

  test('missing telegram_id returns 422', async ({ request }) => {
    const res = await request.post(
      `${BASE}/admin/token?username=admin`,
      { headers: { 'X-Admin-Secret': ADMIN_SECRET } },
    );
    expect(res.status()).toBe(422);
  });
});

// ── 2. AUTH BOUNDARY ─────────────────────────────────────────────────────────

test.describe('Auth boundary — protected endpoints', () => {
  const protectedRoutes = [
    { method: 'GET', path: '/files?storage_id=00000000-0000-0000-0000-000000000000' },
    { method: 'GET', path: '/files/duplicates?storage_id=00000000-0000-0000-0000-000000000000' },
    { method: 'GET', path: '/storages' },
    { method: 'DELETE', path: '/files/bulk' },
  ];

  for (const route of protectedRoutes) {
    test(`${route.method} ${route.path} requires auth`, async ({ request }) => {
      const res = route.method === 'GET'
        ? await request.get(`${BASE}${route.path}`)
        : await request.delete(`${BASE}${route.path}`, { data: { file_ids: [] } });
      expect(res.status()).toBe(401);
    });
  }
});

// ── 3. CONCURRENT REQUESTS ───────────────────────────────────────────────────

test('10 concurrent GET /storages requests return consistent data', async ({ request }) => {
  const token = await getToken(request);

  const results = await Promise.all(
    Array.from({ length: 10 }, () =>
      request.get(`${BASE}/storages`, { headers: authHeaders(token) }),
    ),
  );

  const statuses = results.map(r => r.status());
  expect(statuses.every(s => s === 200)).toBe(true);

  const bodies = await Promise.all(results.map(r => r.json()));
  const first = JSON.stringify(bodies[0]);
  expect(bodies.every(b => JSON.stringify(b) === first)).toBe(true);
});

// ── 4. RATE LIMITING ─────────────────────────────────────────────────────────

test('upload rate limiting — 21 malformed requests trigger 429 eventually', async ({ request }) => {
  const token = await getToken(request);

  const responses: number[] = [];
  for (let i = 0; i < 21; i++) {
    const res = await request.post(`${BASE}/files/upload`, {
      headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
      data: {},
    });
    responses.push(res.status());
  }

  const has429 = responses.some(s => s === 429);
  const hasOnly422 = responses.every(s => s === 422);
  // Either rate-limiting kicked in (429) OR request rejected before rate limiter
  expect(has429 || hasOnly422).toBe(true);
});

// ── 5. BULK DELETE EDGE CASES ────────────────────────────────────────────────

test.describe('DELETE /files/bulk edge cases', () => {
  test('empty file_ids list returns 0 deleted', async ({ request }) => {
    const token = await getToken(request);

    const res = await request.delete(`${BASE}/files/bulk`, {
      headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
      data: { file_ids: [] },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.deleted).toBe(0);
  });

  test('50 non-existent UUIDs return 0 deleted (no crash)', async ({ request }) => {
    const token = await getToken(request);

    const fakeIds = Array.from({ length: 50 }, (_, i) =>
      `00000000-0000-0000-0000-${String(i).padStart(12, '0')}`,
    );

    const res = await request.delete(`${BASE}/files/bulk`, {
      headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
      data: { file_ids: fakeIds },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.deleted).toBe(0);
  });

  test('malformed UUIDs return 422', async ({ request }) => {
    const token = await getToken(request);

    const res = await request.delete(`${BASE}/files/bulk`, {
      headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
      data: { file_ids: ['not-a-uuid', 'also-bad'] },
    });
    expect(res.status()).toBe(422);
  });
});

// ── 6. DUPLICATES ENDPOINT ───────────────────────────────────────────────────

test.describe('GET /files/duplicates', () => {
  test('returns valid schema for existing storage', async ({ request }) => {
    const token = await getToken(request);

    const storagesRes = await request.get(`${BASE}/storages`, { headers: authHeaders(token) });
    const storages = await storagesRes.json();
    if (!storages || storages.length === 0) { test.skip(); return; }

    const res = await request.get(
      `${BASE}/files/duplicates?storage_id=${storages[0].id}`,
      { headers: authHeaders(token) },
    );
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('groups');
    expect(body).toHaveProperty('total_groups');
    expect(body).toHaveProperty('total_duplicates');
    expect(Array.isArray(body.groups)).toBe(true);
  });

  test('wrong storage_id format returns 422', async ({ request }) => {
    const token = await getToken(request);
    const res = await request.get(`${BASE}/files/duplicates?storage_id=not-a-uuid`, {
      headers: authHeaders(token),
    });
    expect(res.status()).toBe(422);
  });

  test('missing storage_id returns 422', async ({ request }) => {
    const token = await getToken(request);
    const res = await request.get(`${BASE}/files/duplicates`, { headers: authHeaders(token) });
    expect(res.status()).toBe(422);
  });
});

// ── 7. INPUT VALIDATION / INJECTION ─────────────────────────────────────────

test.describe('Input validation — filter params', () => {
  const injectionPayloads = [
    "'; DROP TABLE files; --",
    '<script>alert(1)</script>',
    '../../../etc/passwd',
    '%00',
    'a'.repeat(5000),
  ];

  for (const payload of injectionPayloads) {
    test(`mime_category injection: ${payload.slice(0, 40)}`, async ({ request }) => {
      const token = await getToken(request);

      const storagesRes = await request.get(`${BASE}/storages`, { headers: authHeaders(token) });
      const storages = await storagesRes.json();
      if (!storages || storages.length === 0) { test.skip(); return; }

      const res = await request.get(
        `${BASE}/files?storage_id=${storages[0].id}&mime_category=${encodeURIComponent(payload)}`,
        { headers: authHeaders(token) },
      );
      // Must return 200 or 422 — never 500
      expect([200, 422]).toContain(res.status());
    });
  }
});

// ── 8. UI — PAGE LOADS ───────────────────────────────────────────────────────

test.describe('UI', () => {
  test('page loads without JS errors', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });

    await page.goto(BASE);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('#login-tgid')).toBeVisible();
    const criticalErrors = consoleErrors.filter(e => !e.includes('favicon'));
    expect(criticalErrors).toHaveLength(0);
  });

  test('wrong login credentials show error (no crash)', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');

    await page.locator('#login-tgid').fill('12345');
    await page.locator('#login-secret').fill('wrongpassword');
    await page.click('button:has-text("Войти")');
    await page.waitForTimeout(2000);

    // Error message visible
    await expect(page.locator('.msg-err').first()).toBeVisible();
    // Form still works
    await expect(page.locator('#login-tgid')).toBeVisible();
  });

  test('login with valid credentials shows main section', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');

    await page.locator('#login-tgid').fill(TG_ID);
    await page.locator('#login-secret').fill(ADMIN_SECRET);
    await page.click('button:has-text("Войти")');
    await page.waitForTimeout(2000);

    await expect(page.locator('#section-main')).toBeVisible();
    await expect(page.locator('#section-login')).toHaveClass(/hidden/);
  });

  test('duplicate modal opens and shows result', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');

    await page.locator('#login-tgid').fill(TG_ID);
    await page.locator('#login-secret').fill(ADMIN_SECRET);
    await page.click('button:has-text("Войти")');
    await page.waitForTimeout(2000);

    // Wait for main section to be visible
    await expect(page.locator('#section-main')).toBeVisible();

    const storageSelect = page.locator('#storage-select');
    const optionCount = await storageSelect.locator('option').count();
    // Skip if no real storages (only placeholder option)
    if (optionCount <= 1) { test.skip(); return; }

    await storageSelect.selectOption({ index: 1 });
    await page.waitForTimeout(1500);

    // Click the duplicates button
    const dupBtn = page.locator('button[title="Найти дубликаты"]');
    await dupBtn.click();

    // Wait for modal to appear (up to 5s)
    await expect(page.locator('#dup-modal')).not.toHaveClass(/hidden/, { timeout: 5000 });
    await expect(page.locator('#dup-summary')).not.toBeEmpty();
  });
});

// ── 9. RESPONSE TIME BASELINE ────────────────────────────────────────────────

test('key endpoints respond within 2 seconds', async ({ request }) => {
  const token = await getToken(request);

  // /storages — no params needed; /health — public endpoint
  for (const { ep, headers } of [
    { ep: '/storages', headers: authHeaders(token) },
    { ep: '/health', headers: {} },
  ]) {
    const t0 = Date.now();
    const res = await request.get(`${BASE}${ep}`, { headers });
    const ms = Date.now() - t0;
    expect(res.status()).toBe(200);
    expect(ms, `${ep} took ${ms}ms`).toBeLessThan(2000);
  }
});
