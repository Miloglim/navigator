const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    await page.route('**/api/**',route=>route.fulfill({json:{ok:true,data:{rows:[]}}}));
    await page.route('http://127.0.0.1:8795/',route=>route.fulfill({contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')}));
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    await page.evaluate(()=>{
      S.auth={user_id:'quote-tab-state-fixture',role:'sales',views:['sales']};
      S.role='sales';
      applyAccountAccess();
      $('auth-modal').hidden=true;
      setCatTab(document.querySelector('#cat-tabs button[data-tab="quote"]'));
    });
    const tab=page.locator('#cat-tabs button[data-tab="quote"]');
    const state=()=>tab.evaluate(el=>{
      const badge=el.querySelector('.quote-tab-count');
      return {count:badge.textContent,hidden:badge.hidden,hasItems:el.classList.contains('has-items'),
        titleColor:getComputedStyle(el).color,titleWeight:getComputedStyle(el).fontWeight,
        badgeBackground:getComputedStyle(badge).backgroundColor,badgeColor:getComputedStyle(badge).color};
    });
    assert.equal((await state()).hasItems,false);
    await page.evaluate(()=>{
      quoteEditorSelect('rates',{id:'rate-fixture',carrier:'PIL',pol:'Qingdao',pod:'Santos'},true);
      renderQuotePage();
    });
    assert.deepEqual(await state(),{
      count:'1',hidden:false,hasItems:true,titleColor:'rgb(15, 43, 70)',titleWeight:'700',
      badgeBackground:'rgb(232, 97, 77)',badgeColor:'rgb(255, 255, 255)'
    });
    await page.evaluate(()=>quoteEditorAddRow('space'));
    assert.equal((await state()).count,'2');
    const badgeMetrics=()=>tab.evaluate(el=>{
      const badge=el.querySelector('.quote-tab-count'),number=badge.querySelector('.quote-tab-count-number');
      const outer=badge.getBoundingClientRect(),inner=number.getBoundingClientRect();
      return {width:outer.width,height:outer.height,
        horizontalOffset:(inner.left+inner.width/2)-(outer.left+outer.width/2),
        verticalOffset:(inner.top+inner.height/2)-(outer.top+outer.height/2)};
    });
    const oneDigit=await badgeMetrics();
    assert.equal(oneDigit.width,16);
    assert.equal(oneDigit.height,16);
    assert(Math.abs(oneDigit.horizontalOffset)<.1 && Math.abs(oneDigit.verticalOffset)<1);
    await tab.evaluate(el=>el.classList.remove('active'));
    assert.equal((await state()).titleColor,'rgb(15, 43, 70)','the title remains emphasized while another tab is active');
    await tab.evaluate(el=>el.classList.add('active'));
    fs.mkdirSync('.work',{recursive:true});
    await tab.screenshot({path:'.work/quote-tab-state-1440.png'});
    await page.setViewportSize({width:390,height:844});
    assert.equal((await state()).count,'2');
    await tab.screenshot({path:'.work/quote-tab-state-390.png'});
    await page.evaluate(()=>{
      const draft=quoteEditorLoad();
      for(let index=0;index<8;index++)draft.rates.rows.push({id:'extra-'+index,cells:{}});
      quoteEditorSave();updateQuoteSelectionCount();
    });
    assert.equal((await state()).count,'10');
    const twoDigits=await badgeMetrics();
    assert(twoDigits.width>=twoDigits.height && Math.abs(twoDigits.horizontalOffset)<1 && Math.abs(twoDigits.verticalOffset)<1);
    await tab.screenshot({path:'.work/quote-tab-state-10.png'});
    await page.reload({waitUntil:'networkidle'});
    await page.evaluate(()=>{
      S.auth={user_id:'quote-tab-state-fixture',role:'sales',views:['sales']};
      S.role='sales';
      applyAccountAccess();
      $('auth-modal').hidden=true;
      updateQuoteSelectionCount();
    });
    assert.equal((await state()).count,'10','a saved quote draft should restore the tab count after reload');
    await page.evaluate(()=>quoteEditorClear());
    const empty=await state();
    assert.equal(empty.hidden,true);
    assert.equal(empty.hasItems,false);
    console.log('PASS: quote tab count, emphasis, and empty reset at desktop and mobile widths');
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
