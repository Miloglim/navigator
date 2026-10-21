const {chromium}=require('playwright');
const assert=require('assert');
const fs=require('fs');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const rows=[{kind:'carrier',local_name:'MSK',company_id:4550,company_code:'MAERSK',company_name:'Maersk',status:'pending',revision:1,updated_at:1791950400,note:''}];
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/api/**',async route=>{
      const req=route.request(),path=new URL(req.url()).pathname;
      let data={};
      if(path==='/api/admin/company-mappings') data={rows,choices:{port:['Pecém'],carrier:['MSK']}};
      else if(path.endsWith('/company-mappings/save')) {
        const form=req.postDataJSON(),row=rows.find(r=>r.kind===form.kind&&r.local_name===form.local_name);
        assert.equal(form.revision,row.revision);
        Object.assign(row,form,{revision:row.revision+1}); data=row;
      } else if(path.endsWith('/company-mappings/probe')) data=rows[0];
      await route.fulfill({json:{ok:true,data}});
    });
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    await page.evaluate(()=>{
      S.auth={user_id:'fixture-admin',role:'admin',views:['config']};
      S.role='config'; applyAccountAccess();
      document.getElementById('auth-modal').hidden=true;
      document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id==='view-config'));
    });
    await page.locator('[data-tab="company-mappings"]').click();
    await page.locator('#cm-list button').first().waitFor();
    await page.getByRole('button',{name:'核对确认',exact:true}).click();
    assert.equal(await page.locator('#cm-local').inputValue(),'MSK');
    await page.locator('#cm-id').fill('4551');
    await page.locator('#cm-save').click();
    await page.waitForFunction(()=>document.getElementById('cm-status').textContent.startsWith('已确认'));
    assert.equal(rows[0].company_id,'4551');
    assert.equal(rows[0].status,'confirmed');
    await page.getByRole('button',{name:'停用',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('cm-status').textContent.startsWith('已停用'));
    assert.equal(rows[0].status,'disabled');
    await page.locator('#cm-filter').selectOption('pending');
    assert.equal(await page.locator('#cm-count').textContent(),'0 条');
    await page.locator('#cm-filter').selectOption('');
    await page.locator('#cm-search').fill('MAERSK');
    assert.equal(await page.locator('#cm-count').textContent(),'1 条');
    await page.getByRole('button',{name:'编辑',exact:true}).click();
    await page.locator('#cm-save').click();
    await page.waitForFunction(()=>document.getElementById('cm-status').textContent.startsWith('已确认'));
    fs.mkdirSync('.work/company-mappings',{recursive:true});
    await page.screenshot({path:'.work/company-mappings/desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'.work/company-mappings/mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: rendered mapping tab, edit, confirm, disable, restore, filter and search (fixture API).');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
