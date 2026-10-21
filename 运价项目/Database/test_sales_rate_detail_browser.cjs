const {chromium}=require('playwright');
const assert=require('assert');
const fs=require('fs');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    fs.mkdirSync('.work',{recursive:true});
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    await page.evaluate(()=>{
      S.auth={user_id:'fixture',role:'sales',views:['sales']};
      S.role='sales';
      applyAccountAccess();
      $('auth-modal').hidden=true;
      switchRole('sales');
      S.sales.tab='rates';
      const q={entry_id:'rate-42',product_id:42,pol:'SHANGHAI',pod:'SANTOS',carrier:'CMA',
        service_name:'SEAS2',destination_terminal:'SANTOS',validity_raw:'10.22-10.31',
        free_days:'14',transit_days:'38',dead_freight:'USD 100',source_group:'华南运价群',
        sender:'航管',message_text:'SHANGHAI - SANTOS\nUSD 2800/3800',remark:'Subject to space'};
      S.sales.groups=[{current:q,history:[]}];
      $('sales-table-wrap').hidden=false;
      $('table-container').innerHTML='<table class="sales-rate-table"><tbody><tr class="qrow" data-did="0-c"><td><button class="sales-disclosure" data-did="0-c" data-open="0" aria-expanded="false" onclick="toggleDetail(this)">详情</button></td></tr>'+detailRow(q,0,false,'0-c')+'</tbody></table>';
    });
    await page.locator('#table-container .sales-disclosure').click();
    const detail=page.locator('#table-container .sales-local-rate-detail');
    assert(await detail.isVisible(),'local rate detail opens');
    const sections=await detail.locator('.company-detail-body > .company-detail-section, .company-detail-body > .sales-detail-terms-card').evaluateAll(nodes=>nodes.map(node=>node.className));
    assert(sections.findIndex(name=>name.includes('sales-detail-terms-card'))<sections.findIndex(name=>name.includes('detail-raw')),'route-service block stays above the original message');
    assert(sections.findIndex(name=>name.includes('detail-raw'))<sections.findIndex(name=>name.includes('sales-rate-source')),'original message precedes source metadata');
    assert(await detail.locator('.company-detail-header').count()===1,'local detail has the same summary header as company rates');
    for(const width of [1440,390]) {
      await page.setViewportSize({width,height:900});
      const result=await detail.evaluate(node=>{
        const top=node.querySelector('.sales-detail-terms-card');
        const source=node.querySelector('.sales-rate-source-fields');
        return {topBackground:getComputedStyle(top).backgroundColor,
          sourceBackground:getComputedStyle(source).backgroundColor,
          sourceColumns:getComputedStyle(source).gridTemplateColumns.split(' ').length,
          overflow:node.scrollWidth>node.clientWidth+1};
      });
      assert.equal(result.topBackground,'rgb(238, 245, 252)','route-service block keeps its existing blue background');
      assert.equal(result.sourceBackground,'rgba(0, 0, 0, 0)','lower details use the white company detail body');
      assert.equal(result.sourceColumns,width===390?1:2,'source details match online carrier density and mobile width');
      assert(!result.overflow,`local rate detail overflows at ${width}px`);
      await detail.screenshot({path:`.work/local-rate-detail-${width}.png`});
    }
    assert((await detail.innerText()).includes('SHANGHAI - SANTOS'),'source message remains visible');
    assert((await detail.innerText()).includes('Subject to space'),'remark remains visible');
    await detail.locator('.company-detail-close').click();
    assert(await page.locator('#table-container .drow').isHidden(),'local summary header closes its detail');
    console.log('PASS: local rate detail uses the company panel while retaining the route-service block.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
