const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors=[]; page.on('pageerror', error => errors.push(error.message));
    await page.route('https://fonts.googleapis.com/**', route => route.abort());
    await page.route('https://fonts.gstatic.com/**', route => route.abort());
    await page.route('http://127.0.0.1:8795/', route => route.fulfill({contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')}));
    await page.route('**/api/**', route => {
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/auth/status') return route.fulfill({json:{ok:true,authenticated:true,auth_mode:'center',setup_required:false,features:{message_board:false},user:{user_id:'currency-admin',role:'admin',views:['sales','ops','config','account'],name:'currency-admin',display_name:'币种测试'}}});
      return route.fulfill({json:{ok:true,data:{rows:[],groups:[],total:0}}});
    });
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'domcontentloaded'});
    const result=await page.evaluate(() => {
      S.auth={user_id:'currency-admin',role:'admin',views:['sales','ops','config','account'],name:'currency-admin',display_name:'币种测试'};
      S.role='admin'; S.opts={rates:{},space:{}};
      $('auth-modal').hidden=true;
      const holder=document.createElement('div');holder.id='rate-fields';document.body.append(holder);
      renderRateForm({});
      const currency=$('rate-fields').querySelector('select[data-key="币种"]');
      const freight=$('rate-fields').querySelector('[data-key="海运费USD"]');
      const labels=[...$('rate-fields').querySelectorAll('label')].map(x=>x.textContent.trim());
      currency.value='EUR'; freight.value='1250';
      const collected=collectFormFields('rate-fields');
      const rateIndex=RATE_FIELDS.findIndex(f=>f.key==='freight_usd');
      return {nextField:RATE_FIELDS[rateIndex+1].key,collected,labels,
        eurPrice:quotePrices({currency:'EUR',cab:{'20GP':'1250'}})[0],
        usdPrice:quotePrices({cab:{'20GP':'990'}})[0],
        hkdPrice:currencyPriceText('HKD','8800')};
    });
    const freshDefault=await page.evaluate(() => {renderRateForm({});return $('rate-fields').querySelector('select[data-key="币种"]').value;});
    async function inspectPriceLayout() {
      await page.evaluate(() => {
        document.getElementById('rate-layout-fixture')?.remove();
        const fixture=document.createElement('div');
        fixture.id='rate-layout-fixture';
        fixture.className='pd-group';
        fixture.style.width='min(720px, calc(100vw - 24px))';
        fixture.innerHTML='<div class="pd-body"><div class="pd-table-scroll"><table class="pd-gtable pd-rate-table">'
          +'<colgroup><col style="width:15%"><col style="width:7%"><col style="width:8%"><col style="width:8%"><col style="width:8%"><col style="width:8%"><col style="width:10%"><col style="width:7%"><col style="width:6%"><col style="width:7%"><col style="width:16%"></colgroup>'
          +'<thead><tr><th>起运 → 目的</th><th>船司</th><th>20GP</th><th>40HQ/HC</th><th>40NOR</th><th>45HQ/HC</th><th>有效期</th><th>ETD</th><th>F/T</th><th>航线</th><th>操作</th></tr></thead>'
          +'<tbody><tr><td>上海 → 洛杉矶</td><td>ONE</td><td class="cab-cell cab-fail">'+pdRatePriceText({"海运费USD":"3450"})+'</td><td class="cab-cell cab-fail">'+pdRatePriceText({币种:'EUR',"海运费USD":"4450"})+'</td><td class="cab-empty">—</td><td class="cab-empty">—</td><td>2026-10-23 / 2026-10-31</td><td>2026-10-25</td><td>14天</td><td>直达</td><td>编辑 驳回 删除 放行</td></tr></tbody>'
          +'</table></div></div>';
        document.body.append(fixture);
      });
      return page.evaluate(() => {
        const fixture=document.getElementById('rate-layout-fixture');
        const scroller=fixture.querySelector('.pd-table-scroll');
        const cells=fixture.querySelectorAll('.cab-cell');
        const style=getComputedStyle(cells[0]);
        const table=fixture.querySelector('table');
        const tableStyle=getComputedStyle(table);
        return {
          prices:[...cells].map(cell=>cell.textContent),
          whiteSpace:style.whiteSpace,
          overflow:style.overflow,
          textOverflow:style.textOverflow,
          tableWidth:table.getBoundingClientRect().width,
          tableMinWidth:tableStyle.minWidth,
          scrollerWidth:scroller.clientWidth,
          scrollWidth:scroller.scrollWidth,
          fits:[...cells].every(cell=>cell.scrollWidth<=cell.clientWidth+1)
        };
      });
    }
    const desktopLayout=await inspectPriceLayout();
    await page.setViewportSize({width:390,height:844});
    const mobileLayout=await inspectPriceLayout();
    assert.equal(freshDefault,'USD');
    assert.equal(result.nextField,'currency');
    assert.equal(result.collected['币种'],'EUR');
    assert.equal(result.collected['海运费USD'],'1250');
    assert(result.labels.some(label=>label.startsWith('海运费') && !label.includes('USD')),'freight label must not hard-code USD');
    assert.equal(result.eurPrice,'€1,250 / 20GP');
    assert.equal(result.usdPrice,'$990 / 20GP');
    assert.equal(result.hkdPrice,'HK$8800');
    for (const layout of [desktopLayout,mobileLayout]) {
      assert.deepEqual(layout.prices,['$3450','€4450']);
      assert.equal(layout.whiteSpace,'nowrap');
      assert.equal(layout.overflow,'visible');
      assert.equal(layout.textOverflow,'clip');
      assert.ok(layout.tableWidth>=1000,'rate table keeps readable minimum width: '+JSON.stringify(layout));
      assert.ok(layout.scrollWidth>layout.scrollerWidth,'narrow rate table scrolls instead of shrinking text: '+JSON.stringify(layout));
      assert.equal(layout.fits,true,'currency and amount remain fully visible: '+JSON.stringify(layout));
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: rate currency defaults to USD, preserves EUR, and stays complete in desktop/mobile tables.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode=1;});
