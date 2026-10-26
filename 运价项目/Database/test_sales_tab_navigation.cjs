const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

const source = fs.readFileSync('web/index.html', 'utf8');
assert.equal((source.match(/data-tab="space" hidden/g) || []).length, 1, 'space tab must stay hidden');
assert.equal((source.match(/data-tab="dyn" hidden/g) || []).length, 1, 'announcement tab must stay hidden');
assert.equal((source.match(/data-tab="board" hidden/g) || []).length, 1, 'message board must stay hidden unless dev mode enables it');
assert(!source.includes("+action('space','舱位',item.space)"), 'source-group cards must not expose the hidden space page');
assert(source.includes("if (!['rates','space'].includes(kind) || SALES_HIDDEN_TABS.has(kind)) return;"), 'group jumps must reject hidden pages');
assert(source.includes('onclick="bookmarkCurrentPage()"'), 'sales help footer must expose the bookmark action');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    await page.route('http://127.0.0.1:8795/', route => route.fulfill({
      contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')
    }));
    await page.goto('http://127.0.0.1:8795/', {waitUntil:'networkidle'});
    assert.equal(await page.locator('.bookmark-page').innerText(),'点我一键收藏本网页');
    const multiPortTags=await page.evaluate(() => {
      S.sales.polTags=[];
      S.portIdx=new Map([['shanghai','Shanghai'],['ningbo','Ningbo'],['shenzhen','Shenzhen']]);
      addTag('pol','Shanghai,Ningbo,Shenzhen');
      return [...S.sales.polTags];
    });
    assert.deepEqual(multiPortTags,['Shanghai','Ningbo','Shenzhen'],'pasting multiple departure ports must create one tag per port');
    const boardHidden=await page.evaluate(() => {
      S.features={message_board:false};applyFeatureFlags();
      const board=document.querySelector('#cat-tabs [data-tab="board"]');
      setCatTab(board);
      return {hidden:board.hidden,current:currentCatTab()};
    });
    assert.deepEqual(boardHidden,{hidden:true,current:'rates'},'production must not expose or activate the dev message board');
    const bookmark=await page.evaluate(async () => {
      window.__bookmarkCopied='';
      writeClipboardText=async text=>{window.__bookmarkCopied=text;};
      await bookmarkCurrentPage();
      return {copied:window.__bookmarkCopied,url:location.href,toast:$('toast').textContent};
    });
    assert.equal(bookmark.copied,bookmark.url,'bookmark action must copy the exact current URL');
    assert.match(bookmark.toast,/收藏本网页/);
    await page.evaluate(() => {
      S.auth={user_id:'tab-fixture',role:'sales',views:['sales']};
      S.role='sales';
      $('auth-modal').hidden=true;
      switchRole('sales');
      S.sales.queried=false;
      window.navReplies=[];
      const realFetch=window.fetch;
      window.fetch=(input,options) => {
        const path=String(input);
        if (/^\/api\/(rates_grouped|space|notices)\?/.test(path)) {
          return new Promise(resolve => navReplies.push({path,reply:data=>resolve({ok:true,json:async()=>data})}));
        }
        return realFetch(input,options);
      };
      $('table-container').innerHTML='<table id="old-table"><tr><td>OLD TAB</td></tr></table>';
    });

    const hiddenState=await page.evaluate(() => {
      const result={};
      for (const tab of ['space','dyn']) {
        setCatTab(document.querySelector('#cat-tabs [data-tab="'+tab+'"]'));
        result[tab]={current:currentCatTab(),state:S.sales.tab,oldTable:Boolean(document.querySelector('#old-table')),requests:navReplies.length};
      }
      result.hidden=['space','dyn'].map(tab=>document.querySelector('#cat-tabs [data-tab="'+tab+'"]').hidden);
      return result;
    });
    assert.deepEqual(hiddenState.hidden,[true,true]);
    for (const tab of ['space','dyn']) {
      assert.equal(hiddenState[tab].current,'rates',`${tab} must not become the current page`);
      assert.equal(hiddenState[tab].state,'rates',`${tab} must not become the data-page state`);
      assert.equal(hiddenState[tab].oldTable,true,`${tab} must not replace visible rates content`);
      assert.equal(hiddenState[tab].requests,0,`${tab} must not initiate content requests`);
    }

    const defaultState=await page.evaluate(() => {
      S.profile={preferences:{start_tab:'dyn',groups:[]}};
      S.sales.tab='dyn';
      document.querySelectorAll('#cat-tabs button').forEach(button=>button.classList.toggle('active',button.dataset.tab==='dyn'));
      applyPersonalDefaults(true);
      jumpGroup('Hidden space source','space');
      return {
        current:currentCatTab(),
        state:S.sales.tab,
        active:document.querySelector('#cat-tabs button.active')?.dataset.tab,
        requests:navReplies.length
      };
    });
    assert.deepEqual(defaultState,{current:'rates',state:'rates',active:'rates',requests:0},'legacy defaults and hidden group jumps must fall back to visible rates');

    await page.evaluate(() => { salesLoad(); });
    await page.waitForFunction(() => navReplies.length===1);
    assert.match(await page.evaluate(() => navReplies[0].path),/^\/api\/rates_grouped\?/,'visible rates must use the rates endpoint');
    await page.evaluate(() => navReplies.shift().reply({ok:true,total:1,groups:[{
      current:{content_key:'cached-rate',entry_id:'cached-rate',carrier:'MSC',pol:'SHANGHAI',pod:'SANTOS',route:'南美东',message_time:'2026-10-16 10:00',cab:{'20GP':2800}},history:[]
    }]}));
    await page.waitForFunction(() => document.querySelector('#table-container')?.textContent.includes('SANTOS'));

    fs.mkdirSync('.work',{recursive:true});
    await page.screenshot({path:'.work/sales-navigation-rates-1440.png'});
    await page.setViewportSize({width:390,height:844});
    assert.match(await page.locator('#table-container').innerText(),/SANTOS/,'visible rates should remain usable on mobile');
    await page.screenshot({path:'.work/sales-navigation-rates-390.png'});
    console.log('PASS: hidden sales tabs reject direct access, legacy defaults, group jumps, and requests; rates remain usable.');
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
