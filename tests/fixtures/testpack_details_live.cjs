// Read-only verification of the deployed viewer; no database or control API.
const fs = require('fs');
const path = require('path');
const {createHash} = require('crypto');
const {chromium} = require('playwright');

(async () => {
  const [url, output] = process.argv.slice(2);
  const allowed = new URL(url);
  if (allowed.protocol !== 'http:' || allowed.hostname !== '127.0.0.1' || !fs.statSync(output).isDirectory()) throw Error('loopback and existing output directory required');
  const executable = ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(fs.existsSync);
  const browser = await chromium.launch({executablePath: executable, headless: true, timeout: 15000, args: ['--disable-gpu']});
  const checks = [], errors = [];
  const check = (ok, label) => {if (!ok) throw Error(label); checks.push(label);};
  let manifest, report;
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === allowed.origin ? route.continue() : route.abort());
    const get = async route => {const r = await page.request.get(new URL(route, allowed).href); check(r.ok(), 'GET ' + route); return r;};
    const manifestBytes = await (await get('/data/manifest.json')).body();
    manifest = JSON.parse(manifestBytes);
    report = await (await get('/data/report.json')).json();
    check(report.pack_id === manifest.pack_id && report.pack_sha256 === createHash('sha256').update(manifestBytes).digest('hex'), 'report package and digest bound');
    await page.goto(url, {waitUntil: 'networkidle'});
    await page.selectOption('#filter', 'cycle');
    async function scene(id) {
      await page.getByRole('button', {name: '功图周期 · ' + id, exact: true}).click();
      await page.waitForFunction(value => document.querySelector('#scene h2')?.textContent === value && !!document.querySelector('[data-area-status]'), id);
      return (await get('/data/cases/' + id + '.json')).json();
    }
    const base = await scene('cycle-base');
    check((await page.locator('#scene').textContent()).includes('TESTPACK-PUMP-001'), 'synthetic device shown');
    check(base.capture.cycle_duration_s === 8 && base.capture.strokes_per_min === 7.5, 'native cycle and stroke cadence');
    check(base.reference_summary.area_kj > 0 && !(await page.locator('[data-area-status]').textContent()).includes('不可用'), 'complete reference area shown');
    check(await page.locator('[data-trace-toggle="prediction"]').isDisabled(), 'unapproved curve disabled');
    const hit = page.locator('[data-chart-kind="comparison"] [data-chart-hit]');
    await hit.focus(); await hit.press('Home');
    check(/位移（m） -?\d+\.\d{2} · 载荷（kN） -?\d+\.\d{2}/.test(await page.locator('[data-chart-kind="comparison"] [role="status"]').textContent()), 'two decimal coordinates');
    await page.locator('[data-trace-toggle="reference"]').uncheck();
    check(await page.locator('[data-series="reference"]').evaluate(e => e.style.display === 'none'), 'reference toggle hides curve');
    await page.locator('[data-trace-toggle="reference"]').check();
    await page.screenshot({path: path.join(output, 'cycle-base.png'), fullPage: true});
    const noise = await scene('cycle-noise');
    check(noise.quality_summary.status === 'usable_samples' && noise.scene.perturbation === 'noise', 'noise distinct from sample quality');
    check(noise.reference_summary.area_kj === null && (await page.locator('[data-area-status]').textContent()).includes('首末点未闭合'), 'noise endpoints not silently closed');
    const missing = await scene('cycle-missing');
    check(missing.quality_summary.channels.find(c => c.role === 'active_power').missing === 20, '20 missing power samples');
    check(missing.reference_summary.area_kj === base.reference_summary.area_kj, 'power gap does not erase reference area');
    check((await page.locator('.line-chart [data-native-curve]').getAttribute('d')).split('M').length === 3, 'power gap split into two segments');
    await page.screenshot({path: path.join(output, 'cycle-missing.png'), fullPage: true});
    const clipped = await scene('cycle-clipped');
    const q = clipped.quality_summary.channels.find(c => c.role === 'active_power');
    check(q.abnormal > 0 && q.valid + q.missing + q.abnormal === q.total, 'clipped quality accounting');
    const shifted = await scene('cycle-shifted');
    check(shifted.scene.perturbation === 'shifted' && shifted.quality_summary.channels.find(c => c.role === 'active_power').missing === 2, 'shift condition and missing tail');
    await scene('cycle-speed');
    const panel = await page.locator('#scene [data-budget-panel]').textContent();
    const actual = report.checks.find(c => c.check_id === 'cycle-speed-approved-inversion' && c.layer === 'numeric');
    if (actual) {
      check(panel.includes('207.6222') && panel.includes('1.000000e-5') && panel.includes('3.515651e-5'), 'fixed budget and small error retained');
      check(panel.includes('未提交参数'), 'blocked parameter submission');
    } else {
      check(panel.includes('没有该样本的逐项反演结果') && panel.includes('全包反演比较：未运行') && !panel.includes('207.6222'), 'extended report not_run is not borrowed result');
    }
    await page.locator('#scene [data-budget-panel]').screenshot({path: path.join(output, 'budget.png')});
    // Download bytes, not the already decoded JSON, establish raw-file traceability.
    const attachment = base.downloads.find(d => d.path.endsWith('.owpwave'));
    const raw = await (await get('/pack/' + attachment.path)).body();
    check(createHash('sha256').update(raw).digest('hex') === attachment.sha256, 'download matches manifest digest');
    await page.selectOption('#filter', 'current');
    await page.waitForSelector('[data-quality-table]');
    await page.waitForFunction(() => document.querySelector('#scene h2')?.textContent.startsWith('current-'));
    check(await page.locator('.line-chart svg').count() === 3, 'three raw current channels');
    check(!(await page.locator('.state-grid').textContent()).includes('反演'), 'current status not mislabeled as load inversion');
    await page.locator('#report-tab').click();
    check((await page.locator('#report').textContent()).includes('页面升级不代表重新执行'), 'historical report retained');
    await page.locator('#scene-tab').click(); await page.selectOption('#filter', 'cycle'); await scene('cycle-base');
    await page.setViewportSize({width: 780, height: 1000});
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no page overflow at narrow viewport');
    await page.screenshot({path: path.join(output, 'narrow.png'), fullPage: true});
    check(errors.length === 0, 'no browser errors');
    const result = {ok: true, checks, pack_id: manifest.pack_id, historical_report_status: report.status, scope: 'read-only viewer and download checks only', field_validation: false};
    fs.writeFileSync(path.join(output, 'viewer-checks.json'), JSON.stringify(result, null, 2), {flag: 'wx'});
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    process.stdout.write(JSON.stringify({ok: false, checks, error: error.message, browser_errors: errors})); process.exitCode = 1;
  } finally {await browser.close();}
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});
