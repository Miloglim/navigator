const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

const root = {
  id: 'root-1', kind: 'group', title: '测试群 A', status: 'RUNNING', mode: 'production-batch',
  group_name: '测试群 A', pull_time: 1792742404, pull_status: 'SUCCEEDED', child_nodes: [
    {id: 'child-1', kind: 'message', title: '消息 1', status: 'DONE'},
    {id: 'child-2', kind: 'message', title: '消息 2', status: 'RUNNING'},
  ],
  children: [
    {id: 'child-1', lane: 'message:group-a:msg-1', failed: 0, waiting: 0, partial_review: 0},
    {id: 'child-2', lane: 'message:group-a:msg-2', failed: 0, waiting: 1, partial_review: 0},
  ],
  tasks: [
    {id: 'pull-1', stage: 'pull', title: '拉取', status: 'SUCCEEDED', summary: '完成', result: {}},
    {id: 'fanout-1', stage: 'fanout', title: '拆消息', status: 'RUNNING', summary: '执行中', result: {}},
  ],
  events: [],
};
const reviewRoot = {
  ...root, id: 'review-root', title: '测试群 A', status: 'DONE', display_status: 'PARTIAL_REVIEW',
  review_needed: true, has_issue: true, working: false,
  child_nodes: [{id: 'review-child', kind: 'message', title: '历史部分待核消息', status: 'PARTIAL_REVIEW'}],
  children: [{id: 'review-child', lane: 'message:group-a:review', failed: 0, waiting: 0, partial_review: 1}],
};

function child(id, title, status, displayStatus, displayLabel, taskStatus) {
  return {
    id, kind: 'message', title, status, display_status: displayStatus, display_label: displayLabel, group_name: '测试群 A',
    working: taskStatus === 'QUEUED',
    tasks: [{id: id + '-extract', stage: 'extract', title: '报价抽取', status: taskStatus, summary: taskStatus === 'SUCCEEDED' ? '完成' : '', result: {}}],
    events: [], source: {text: '测试原文', sender: '测试账号', message_time: '2026-10-23 16:00:00'},
  };
}

const runtime = {
  groups: [
    {shard: 'A', name: '测试群 A', slug: 'group-a'},
    {shard: 'B', name: '测试群 B', slug: 'group-b'},
  ],
  workers: {control_enabled: true, service_ready: true, capacity: 1, agent: 'fixture', configured: [], stages: [], queue: {by_status: {}}},
  dws: {state: 'authenticated'}, agent: {state: 'ready'}, service: {state: 'running'},
  scheduler: {
    enabled: true, scheduler_alive: true, tick_seconds: 10, windows: [{start: '09:00', end: '18:59', interval_seconds: 1800}], weekdays: [1, 2, 3, 4, 5],
    shard_offsets_seconds: {A: 0, B: 300},
    next_slots: {A: {at: '2026-10-26T16:30:00+08:00', trigger: 'engine:20261026-163000:A'}, B: {at: '2026-10-26T16:35:00+08:00', trigger: 'engine:20261026-163500:B'}},
  },
  triggers: [], pipeline: [], settings: {worker: {}, schedule: {}},
};

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const errors = [];
    let triggerBody = null;
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.route('https://fonts.googleapis.com/**', route => route.abort());
    await page.route('https://fonts.gstatic.com/**', route => route.abort());
    await page.route('http://127.0.0.1:8797/studio**', route => route.fulfill({contentType: 'text/html', body: fs.readFileSync('web/studio.html', 'utf8')}));
    await page.route('**/api/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname === '/api/health') return route.fulfill({json: {ok: true, data: {worker_alive: true}}});
      if (url.pathname === '/api/runtime') return route.fulfill({json: {ok: true, data: runtime}});
      if (url.pathname === '/api/runtime/trigger') {
        triggerBody = request.postDataJSON();
        return route.fulfill({json: {ok: true, data: {group: runtime.groups[1], trigger_key: 'manual:test', run_ids: ['root-1']}}});
      }
      if (url.pathname === '/api/trace/roots') return route.fulfill({json: {ok: true, data: {runs: [{...root, tasks: undefined, events: undefined, review_root: {...reviewRoot, tasks: undefined, events: undefined}}]}}});
      if (url.pathname === '/api/trace/run') {
        const id = url.searchParams.get('id');
        if (id === 'root-1') return route.fulfill({json: {ok: true, data: root}});
        if (id === 'review-root') return route.fulfill({json: {ok: true, data: reviewRoot}});
        if (id === 'child-1') return route.fulfill({json: {ok: true, data: child('child-1', '消息 1', 'QUEUED', 'QUEUED', '等待执行', 'QUEUED')}});
        if (id === 'child-2') return route.fulfill({json: {ok: true, data: child('child-2', '消息 2', 'DONE', 'COMPLETED', '已完成', 'SUCCEEDED')}});
      }
      return route.fulfill({json: {ok: true, data: {}}});
    });

    await page.goto('http://127.0.0.1:8797/studio', {waitUntil: 'domcontentloaded'});
    await page.locator('.child-row').first().waitFor();
    assert.equal(await page.locator('.root-row').count(), 1, 'one production group must remain one tree row');
    assert.equal(await page.locator('.root-review-link').count(), 1, 'historical review is nested under the group row');
    assert.match(await page.locator('.root-row .root-meta').textContent(), /最近拉取/, 'production group shows its latest pull time');
    await page.locator('.child-row').first().click();
    await page.waitForFunction(() => document.querySelectorAll('.child-row').length === 2 && document.querySelector('.child-row[aria-current="true"]'));
    assert.equal(await page.locator('.child-row').count(), 2, 'clicking a message keeps the group list expanded');
    assert.equal(await page.locator('.root-row').getAttribute('aria-expanded'), 'true');
    assert.match(await page.locator('.child-row[aria-current="true"]').textContent(), /msg-1/);
    assert.match(await page.locator('.child-row[aria-current="true"] .child-state').textContent(), /等待执行/, 'detail state overrides a stale completed summary');
    assert.ok(await page.locator('.trace-map .flow-node.QUEUED').count() > 0, 'graph uses the same queued state as the selected child');
    const selectedBackground = await page.locator('.child-row[aria-current="true"]').evaluate(element => getComputedStyle(element).backgroundColor);
    assert.notEqual(selectedBackground, 'rgba(0, 0, 0, 0)', 'selected child has a visible highlight');

    await page.locator('.tab[data-tab="runtime"]').click();
    await page.locator('.shard-card').first().waitFor();
    assert.equal(await page.locator('.shard-card').count(), 2);
    assert.match(await page.locator('.shard-board').locator('xpath=..').textContent(), /分片编排.*2 个分片 · 2 个群/s);
    assert.match(await page.locator('.shard-card').first().textContent(), /A · 1 个群.*偏移 0 秒 · 0 分钟.*下一触发/s);
    assert.match(await page.locator('.shard-card').nth(1).textContent(), /B · 1 个群.*偏移 300 秒 · 5 分钟.*下一触发/s);
    fs.mkdirSync('ops/reports/trace-shard-board-20261026', {recursive: true});
    await page.locator('.shard-board').screenshot({path: 'ops/reports/trace-shard-board-20261026/shard-board.png'});
    await page.locator('.shard-card').first().locator('summary').click();
    assert.match(await page.locator('.shard-card').first().textContent(), /测试群 A.*group-a/s);
    const groupSelect = page.locator('select[aria-label="选择解析群"]');
    await groupSelect.selectOption('group-b');
    await page.evaluate(() => loadRuntime());
    assert.equal(await groupSelect.inputValue(), 'group-b', 'runtime refresh preserves the selected group');
    await page.getByRole('button', {name: '立即解析'}).click();
    await page.waitForFunction(() => document.querySelector('#run-title')?.textContent?.includes('测试群 A'));
    assert.deepEqual(triggerBody, {group_slug: 'group-b'});
    assert.equal(await page.locator('select[aria-label="选择解析群"]').inputValue(), 'group-b', 'manual trigger keeps the selected group after refresh');
    assert.deepEqual(errors, []);
    console.log('PASS: expanded tree selection persists and manual group trigger keeps selection with visible feedback.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
