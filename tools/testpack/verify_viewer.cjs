const fs=require('fs');
const {chromium}=require('playwright');
(async()=>{
 const [url,packId,screenshot]=process.argv.slice(2);
 const allowed=new URL(url);if(allowed.hostname!=='127.0.0.1'||allowed.protocol!=='http:')throw Error('loopback only');
 const executable=[chromium.executablePath(),'C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(p=>fs.existsSync(p));
 const browser=await chromium.launch({executablePath:executable,headless:true,timeout:15000,args:['--disable-gpu']});
 const checks=[],errors=[];const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN'});
  await page.route('**/*',route=>new URL(route.request().url()).origin===allowed.origin?route.continue():route.abort());
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url,{waitUntil:'networkidle',timeout:20000});
  await page.waitForSelector('#cases button',{timeout:20000});
  await page.selectOption('#filter','cycle');
  await page.waitForSelector('[data-chart-kind="comparison"]',{timeout:20000});
  check((await page.locator('#identity').textContent()).includes(packId),'actual_package_identity');
  check(await page.locator('[data-trace-toggle="prediction"]').isDisabled(),'unapproved_prediction_not_drawn');
  const hit=page.locator('[data-chart-kind="comparison"] [data-chart-hit]');await hit.focus();await hit.press('Home');
  check(/位移（m） -?\d+\.\d{2} · 载荷（kN） -?\d+\.\d{2}/.test(await page.locator('[data-chart-kind="comparison"] [role="status"]').textContent()),'native_coordinate_two_decimals');
  await page.locator('[data-trace-toggle="reference"]').uncheck();
  check(await page.locator('[data-series="reference"]').evaluate(e=>e.style.display==='none'),'reference_toggle');
  await page.locator('[data-trace-toggle="reference"]').check();
  await page.screenshot({path:screenshot,fullPage:true,timeout:10000});
  await page.selectOption('#filter','history');await page.waitForSelector('.chart-pair svg');
  check(await page.locator('.chart-pair svg').count()===2,'liquid_and_energy');
  await page.selectOption('#filter','shape_only');await page.waitForFunction(()=>document.querySelector('#scene').textContent.includes('不计算反演精度'));
  check(!(await page.locator('#scene').textContent()).includes('RMSE'),'shape_only_boundary');
  await page.locator('#report-tab').click();await page.waitForSelector('#report table');
  check((await page.locator('#report').textContent()).includes('硬件验证：未运行'),'hardware_not_claimed');
  check((await page.locator('#report').textContent()).includes('inversion-comparison'),'actual_report_loaded');
  await page.locator('#report').screenshot({path:screenshot.replace(/\.png$/,'-report.png'),timeout:10000});
  check(errors.length===0,'no_page_errors');
  process.stdout.write(JSON.stringify({ok:true,checks,field_validation:false}));
 }catch(error){process.stdout.write(JSON.stringify({ok:false,checks,error_type:error.name,failed_check:error.message.slice(0,500),page_errors:errors}));}
 finally{await browser.close();}
})().catch(()=>{process.exitCode=1;});
