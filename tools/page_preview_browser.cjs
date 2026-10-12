const fs=require('fs'),path=require('path'),{chromium}=require('playwright');
(async()=>{const config=JSON.parse(fs.readFileSync(0,'utf8'));const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true,args:['--disable-gpu']});const result={pages:[],failures:[],outside_preview_requests:[],page_errors:[],overflow:[]};
try{for(const [width,height] of [[1366,768],[1920,1080]]){const context=await browser.newContext({viewport:{width,height},acceptDownloads:true});const page=await context.newPage();page.on('pageerror',e=>result.page_errors.push(e.message));page.on('request',r=>{const u=new URL(r.url());if(u.protocol.startsWith('http')&&(u.origin!==config.url||(u.pathname.startsWith('/api/')&&!u.pathname.startsWith('/api/preview/'))))result.outside_preview_requests.push(r.url());});
await page.goto(config.url);await page.locator('#previewUser').fill(config.username);await page.locator('#previewPassword').fill(config.password);await page.locator('#previewLogin button').click();await page.waitForSelector('.preview-shell');const catalogue=await page.evaluate(async()=> (await(await fetch('/api/preview/catalog')).json()).data);
for(const item of catalogue){await page.evaluate(id=>{location.hash='#/'+id;},item.id);
// Render completion is marked by title; explicit error states are collected below.
try{await page.waitForFunction(({label})=>document.title===label+' · 油井平台页面预览',{label:item.label},{timeout:30000});}catch(e){result.failures.push({id:item.id,width,error:'render timeout',text:(await page.locator('#workspace').innerText()).slice(0,500)});}
const text=await page.locator('#workspace').innerText();if(/页面读取失败|页面正在制作|预览处理失败/.test(text))result.failures.push({id:item.id,width,text:text.slice(0,500)});
const alerts=await page.locator('#workspace .study-alert-error,#workspace .inversion-error,#workspace [role=alert]').allTextContents();for(const alert of alerts)if(alert.trim())result.failures.push({id:item.id,width,error:alert});
if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))result.overflow.push({id:item.id,width});
if(config.screens){fs.mkdirSync(config.screens,{recursive:true});await page.locator('#workspace').evaluate(el=>el.scrollTop=0);await page.screenshot({path:path.join(config.screens,item.id+'-'+width+'.png'),fullPage:true});await page.locator('#workspace').evaluate(el=>el.scrollTop=el.scrollHeight);await page.screenshot({path:path.join(config.screens,item.id+'-'+width+'-bottom.png'),fullPage:true});await page.locator('#workspace').evaluate(el=>el.scrollTop=0);}
result.pages.push({id:item.id,width,characters:text.length});}
await page.goto(config.url+'/assets/open-interface/index.html#hardware');await page.waitForSelector('#hardware');result.open_interface_entries=1;
await context.close();}
}finally{await browser.close();}process.stdout.write(JSON.stringify(result));})().catch(e=>{process.stderr.write(e.stack);process.exit(1);});
