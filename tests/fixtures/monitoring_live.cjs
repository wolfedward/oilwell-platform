// Actual installed HTTP only. No request routing/mocking and no secret argv.
const {chromium,expect}=require('playwright');
(async()=>{
 let input='';for await(const chunk of process.stdin)input+=chunk;
 const options=JSON.parse(input),origin=`http://127.0.0.1:${options.port}`;
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true,args:['--disable-gpu']});
 const evidence=[];
 try{
  for(const width of [1366,1920]){
   const context=await browser.newContext({viewport:{width,height:900},locale:'zh-CN'});
   const split=options.cookie.indexOf('=');
   await context.addCookies([{name:options.cookie.slice(0,split),value:options.cookie.slice(split+1),url:origin,httpOnly:true,sameSite:'Strict'}]);
   const page=await context.newPage(),errors=[],failed=[];
   page.on('pageerror',error=>errors.push(error.message));
   page.on('response',response=>{if(response.url().startsWith(origin+'/api/')&&response.status()>=400)failed.push({status:response.status(),url:response.url()});});
   const routes=[['wells',{},'table'],['well-detail',{well_id:options.well},'[data-well-content]'],
    ['field-registry',{device_id:options.device},'table'],['channel-mapping',{device_id:options.device},'table'],
    ['acquisition',{},'[data-message]'],['data-quality',{},'table'],
    ['well-realtime',{well_id:options.realtime_well||options.well},'[data-latest] table'],
    ['well-history',{well_id:options.well,...options.filters},'[data-history-results] table']];
   for(const [route,params,selector] of routes){
    await page.goto(origin+'/#/'+route+'?'+new URLSearchParams(params),{waitUntil:'networkidle',timeout:15000});
    await page.locator('#workspace').waitFor();
    if(route==='well-detail')await page.getByText('软件链路测试井',{exact:false}).first().waitFor();
    else try{await page.locator('#workspace '+selector).first().waitFor({timeout:10000});}
    catch(error){throw Error(JSON.stringify({route,width,failed,text:await page.locator('#workspace').innerText(),error:error.message}));}
    const text=await page.locator('#workspace').innerText();
    if(text.includes('暂不可用')||text.includes('接口未接入'))throw Error('Actual page unavailable: '+route);
    if(route==='well-history'&&!text.includes('41.123456789'))throw Error('Actual raw history value missing');
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2);
    if(overflow)throw Error('Actual page overflow: '+route+' '+width);
    evidence.push({route,width,actual_http:true});
   }
   if(errors.length||failed.length)throw Error(JSON.stringify({errors,failed}));
   await context.close();
  }
  process.stdout.write(JSON.stringify({pages:evidence}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
