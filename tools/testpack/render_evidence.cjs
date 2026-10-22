const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');
(async () => {
  const [source, target] = process.argv.slice(2);
  if (!source || !target || fs.existsSync(target)) throw new Error('new target required');
  const options = {headless: true};
  if (!fs.existsSync(chromium.executablePath()) && process.platform === 'win32') {
    if (fs.existsSync('C:/Program Files/Google/Chrome/Application/chrome.exe')) options.channel = 'chrome';
    else if (fs.existsSync('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')) options.channel = 'msedge';
  }
  // An ephemeral headless profile only, never the user's existing browser.
  const browser = await chromium.launch(options);
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 900}, deviceScaleFactor: 1});
    await page.route('**/*', route => route.request().url().startsWith('file:') ? route.continue() : route.abort());
    await page.goto(pathToFileURL(source).href);
    await page.evaluate(() => document.fonts.ready);
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    if (height > 16000) throw new Error('page height exceeds evidence bound');
    const bytes = target.endsWith('.pdf')
      ? await page.pdf({format: 'A4', printBackground: true, preferCSSPageSize: true})
      : await page.screenshot({fullPage: true});
    fs.writeFileSync(target, bytes, {flag: 'wx'});
  } finally {await browser.close();}
})().catch(() => {process.exitCode = 1;});
