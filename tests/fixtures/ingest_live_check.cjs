// Real platform UI and HTTP/PG stack; no route interception or fake API.
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');

const PHASES = new Set([
  'input_validation', 'evidence_setup', 'browser_launch', 'page_setup',
  'navigation', 'login', 'case_query', 'raw_preview', 'desktop_capture',
  'case_close', 'audit_query', 'audit_capture', 'laptop_layout',
  'receipt_resend', 'receipt_capture', 'page_errors',
]);
const NETWORK_CODES = [
  ['ERR_UNSAFE_PORT', 'unsafe_port'],
  ['ERR_CONNECTION_REFUSED', 'connection_refused'],
  ['ERR_TIMED_OUT', 'timed_out'],
  ['ERR_NAME_NOT_RESOLVED', 'name_not_resolved'],
  ['ERR_CONNECTION_RESET', 'connection_reset'],
  ['ERR_EMPTY_RESPONSE', 'empty_response'],
];

function safeField(error, field) {
  try {
    return typeof error?.[field] === 'string' ? error[field] : '';
  } catch {
    return '';
  }
}

function summarizeFailure(phase, error) {
  const safePhase = PHASES.has(phase) ? phase : 'unknown';
  const name = safeField(error, 'name');
  const message = safeField(error, 'message');
  let network = 'none';
  if (safePhase === 'navigation') {
    const match = NETWORK_CODES.find(([code]) => message.includes('net::' + code));
    if (match) network = match[1];
    else if (/net::ERR_[A-Z_]+/.test(message)) network = 'other';
  }
  const errorKind = name === 'AssertionError' ? 'assertion' :
    name === 'TimeoutError' ? 'timeout' :
    network !== 'none' ? 'network' :
    name === 'TargetClosedError' ? 'browser_closed' : 'unexpected';
  return {ok: false, phase: safePhase, error_kind: errorKind, network};
}

async function run(setPhase) {
  const [base, evidence] = process.argv.slice(2);
  assert.match(base, /^http:\/\/127\.0\.0\.1:\d+$/);
  setPhase('evidence_setup');
  fs.mkdirSync(evidence, {recursive: true});
  setPhase('browser_launch');
  const {chromium} = require('playwright');
  const executablePath = process.env.OILWELL_TEST_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    setPhase('page_setup');
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
    page.setDefaultTimeout(10000);
    const errors = [], checks = [], screenshots = [];
    page.on('pageerror', () => errors.push('page_error'));
    setPhase('navigation');
    await page.goto(base + '/#/ingest-reliability');
    setPhase('login');
    await page.locator('#username').fill('administrator');
    await page.locator('#password').fill('Admin@2026');
    await page.locator('#loginForm button[type=submit]').click();
    const detail = page.locator('[data-ingest-detail]');
    setPhase('case_query');
    await page.locator('[data-ingest-select="cases"]').first().click();
    await detail.getByText('BROWSER', {exact: true}).waitFor();
    checks.push('live authenticated case query');
    setPhase('raw_preview');
    await page.locator('[data-ingest-raw="preview"]').click();
    const raw = page.locator('[data-ingest-raw-text]');
    await raw.waitFor();
    assert.equal(JSON.parse(await raw.textContent()).message_id, 'BROWSER');
    checks.push('audited exact-text preview');
    const capture = async (label, locator = detail.locator('header')) => {
      const filename = path.join(evidence, label + '.png');
      // The workspace is a nested scrollport. Capturing a taller-than-viewport
      // element includes unpainted/clipped areas; capture the actual viewport.
      await locator.evaluate(element => element.scrollIntoView({block: 'start', behavior: 'instant'}));
      await page.screenshot({path: filename, animations: 'disabled'});
      screenshots.push(label);
    };
    setPhase('desktop_capture');
    await capture('ingest-live-detail-1440');
    await capture('ingest-live-raw-1440', page.locator('.ingest-raw'));
    async function operation(action, reason) {
      await page.locator(`[data-ingest-action="${action}"]`).click();
      const form = page.locator('[data-ingest-action-form]');
      await form.locator('[name=reason]').fill(reason);
      await form.locator('button[type=submit]').click();
      await page.locator('[data-ingest-actions] .ingest-notice').filter({hasText: '操作已提交'}).waitFor();
    }
    setPhase('case_close');
    await operation('close', '软件闭环测试：核对格式错误，保留原始记录');
    await detail.getByText('人工已关闭', {exact: true}).waitFor();
    checks.push('versioned close through real HTTP');
    setPhase('audit_query');
    await page.locator('[data-ingest-audit]').click();
    await page.locator('.ingest-audit-event').filter({hasText: 'raw.preview'}).waitFor();
    await page.locator('.ingest-audit-event').filter({hasText: 'case.close'}).waitFor();
    checks.push('persisted access and operation audit');
    setPhase('audit_capture');
    await capture('ingest-live-audit-1440', page.locator('.ingest-audit'));
    setPhase('laptop_layout');
    await page.setViewportSize({width: 1024, height: 900});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal page overflow');
    await capture('ingest-live-detail-1024');
    checks.push('1024px detail without page overflow');
    setPhase('receipt_resend');
    await page.locator('[data-ingest-select="receipts"]').first().click();
    await operation('resend', '软件闭环测试：请求补发既有回执');
    setPhase('receipt_capture');
    await capture('ingest-live-receipt-1024');
    checks.push('audited resend of immutable receipt');
    setPhase('page_errors');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ok: true, checks, screenshots, page_errors: errors}));
  } finally {
    await browser.close();
  }
}

module.exports = {summarizeFailure};
if (require.main === module) {
  let phase = 'input_validation';
  run(value => { phase = value; }).catch(error => {
    process.stderr.write(JSON.stringify(summarizeFailure(phase, error)) + '\n');
    process.exitCode = 1;
  });
}
