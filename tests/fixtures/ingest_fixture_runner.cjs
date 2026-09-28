// Wait for the actual fixture outcome with a bounded wall-clock deadline.
// A fixed virtual-time dump can exit while asynchronous modules are still pending.
const {chromium} = require('playwright');

(async () => {
  const [url, width, screenshot = ''] = process.argv.slice(2);
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, timeout: 10000,
    args: ['--allow-file-access-from-files', '--disable-gpu'],
  });
  try {
    const page = await browser.newPage({viewport: {width: Number(width), height: 1000}, locale: 'zh-CN'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 15000});
    await page.waitForFunction(() => {
      const value = document.querySelector('#result')?.textContent;
      return value && value !== 'pending';
    }, null, {timeout: 20000});
    const result = JSON.parse(await page.locator('#result').textContent());
    if (errors.length) { result.ok = false; result.pageErrors = errors; }
    if (screenshot) await page.screenshot({path: screenshot, timeout: 5000});
    process.stdout.write(JSON.stringify(result));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
