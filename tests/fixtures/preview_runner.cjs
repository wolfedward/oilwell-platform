const {chromium} = require('playwright');
(async () => {
  const [url, width = '1366', height = '768', screenshot = ''] = process.argv.slice(2);
  const browser = await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless:true,timeout:10000,args:['--allow-file-access-from-files','--disable-gpu']});
  try {
    const page = await browser.newPage({viewport:{width:Number(width),height:Number(height)},locale:'zh-CN'});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(url,{waitUntil:'domcontentloaded',timeout:15000});
    await page.waitForFunction(()=>document.querySelector('#result')?.textContent !== 'pending',null,{timeout:35000});
    const result=JSON.parse(await page.locator('#result').textContent());
    result.page_errors=errors;
    if(screenshot) await page.screenshot({path:screenshot,fullPage:true});
    process.stdout.write(JSON.stringify(result));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
