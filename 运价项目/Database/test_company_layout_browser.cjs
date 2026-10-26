const {chromium}=require('playwright');
const assert=require('assert');
const fs=require('fs');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    fs.mkdirSync('.work',{recursive:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    const remarkDisplay=await page.evaluate(()=>{
      const internal='[部分解析待核] 卡在coverage；明确报价格未覆盖 45 格，示例: [大段内部数据]；来源行=image:1:row:3';
      const html=fieldHtml({key:'remark',label:'备注',type:'textarea',full:true},{remark:internal});
      const host=document.createElement('div');host.innerHTML=html;const textarea=host.querySelector('textarea');
      return {visible:visibleBusinessRemark(internal),shown:textarea.value,internal:textarea.dataset.internalRemark,normal:visibleBusinessRemark('包含 EFS 与 LWS，按原图确认；来源行=image:1:row:4')};
    });
    assert.equal(remarkDisplay.visible,'','internal coverage diagnostics must not render as a business remark');
    assert.equal(remarkDisplay.shown,'','pending editor must not expose internal partial diagnostics');
    assert.equal(remarkDisplay.internal,'来源行=image:1:row:3','source-row evidence remains internal for coverage identity');
    assert.equal(remarkDisplay.normal,'包含 EFS 与 LWS，按原图确认','normal business remarks remain visible');
    await page.evaluate(()=>{
      S.auth={user_id:'fixture',role:'sales',views:['sales']};
      S.role='sales';
      applyAccountAccess();
      $('auth-modal').hidden=true;
      switchRole('sales');
      $('sales-table-wrap').hidden=false;
      $('company-rate-results').hidden=false;
      $('sales-source-local').hidden=true;
      $('sf-ctype').value='40HQ/HC';
      renderCompanyTask('company',{status:'complete',results:[{fresh:true,items:[{
        product_id:42,carrier_info:{carrier_code:'MAERSK'},line_code:'ASAS2',trans_day:26,
        valid_from:1792627200000,valid_to:1793404800000,
        product_price:[{price_unit:'40HQ',currency_code:'USD',show_price:5614}],
        comments_list:[{productId:42,commentsSchedule:[{schedule:1,line_code:'EF4'},{schedule:4,line_code:'EL2'}],comments:'Price subject to space confirmation',profitDescription:'USD 0-50',internal_comments:'Confirm before quoting'}],
        from_terminal_info:{terminal_en_name:'SHANGHAI'},to_terminal_info:{terminal_en_name:'NAVEGANTES'}
      }]}]});
      renderCompanyTask('realtime',{status:'complete',query:{container_id:12},results:[{fresh:true,items:[{
        carrier:{carrierCode:'CMA'},result:[{
          carrierName:'CMA CGM SHIPPING MEXICO',pol:{terminalEnName:'SHANGHAI'},pod:{terminalEnName:'NAVEGANTES'},
          etd:1792166400000,eta:1794672000000,vessel:'COSCO SHIPPING MEXICO',voyage:'0BDPLW1MA',
          totalOceanPriceUsd:5614,totalPrice:'5614.0USD+1020.0CNY',obtainDate:1792166400000,
          scheduleWeek:'周五',totalPriceDetails:[{name:'燃油附加费',currencyCode:'USD',price:120,containerType:'40HQ',unit:'箱'}],
          deadResult:[{chargeName:'其他费用',currencyCode:'CNY',price:60,cntrSize:'40HQ',chargeModel:'箱'}]
        }]
      }]}]});
    });
    const sourceGroupTab=page.locator('#cat-tabs button[data-tab="group"]');
    assert(await sourceGroupTab.isHidden(),'sales accounts must not see the source-group page');
    assert.equal(await page.evaluate(()=>{
      const button=document.querySelector('#cat-tabs button[data-tab="group"]');
      button.hidden=false;
      setCatTab(button);
      return currentCatTab();
    }),'rates','sales accounts cannot open the source-group page directly');
    await page.evaluate(()=>{
      S.auth={user_id:'fixture-admin',role:'admin',views:['sales','ops','config','account','monitor']};
      applyAccountAccess();
    });
    assert(await sourceGroupTab.isVisible(),'admin accounts can see the source-group page');
    assert(await page.locator('#cat-tabs [data-tab="space"]').isHidden(),'seat tab stays hidden for regular admin');
    assert(await page.locator('#cat-tabs [data-tab="dyn"]').isHidden(),'notice tab stays hidden for regular admin');
    await page.evaluate(()=>{
      S.auth={user_id:'fixture-superadmin',role:'superadmin',views:['sales','ops','config','account','monitor']};
      applyAccountAccess();
    });
    assert(await sourceGroupTab.isVisible(),'superadmin accounts can see the source-group page');
    assert(await page.locator('#cat-tabs [data-tab="space"]').isVisible(),'superadmin accounts can see the seat tab');
    assert(await page.locator('#cat-tabs [data-tab="dyn"]').isVisible(),'superadmin accounts can see the notice tab');
    assert(await page.locator('#cat-tabs [data-tab="board"]').isHidden(),'formal builds keep the development-only message board hidden');
    assert.equal(await page.locator('#ops-ownership-control').evaluate(node=>node.hidden),false,'superadmin accounts can see ownership controls');
    assert.equal(await page.locator('#view-account > .ops-tabs').evaluate(node=>node.hidden),false,'superadmin accounts can see all account tabs');
    assert.equal(await page.locator('#view-account .ops-tabs [data-tab="system"]').evaluate(node=>node.hidden),false,'superadmin accounts can see account administration');
    assert.equal(await page.locator('#view-account .ops-tabs [data-tab="publishers"]').evaluate(node=>node.hidden),false,'superadmin accounts can see publisher administration');
    await page.evaluate(()=>{
      S.auth={user_id:'fixture',role:'sales',views:['sales']};
      applyAccountAccess();
    });
    const hiddenAncestors=await page.locator('#company-rate-company .sales-disclosure').evaluate(button=>{
      const result=[];
      for(let node=button;node;node=node.parentElement){
        if(node.hidden||getComputedStyle(node).display==='none') result.push({id:node.id,tag:node.tagName,hidden:node.hidden,display:getComputedStyle(node).display});
      }
      return result;
    });
    assert.deepEqual(hiddenAncestors,[],`browser fixture has hidden ancestors: ${JSON.stringify(hiddenAncestors)}`);
    await page.locator('#company-rate-company .sales-disclosure').click();
    await page.locator('#company-rate-realtime .sales-disclosure').click();
    assert(await page.locator('#company-rate-company .company-detail-header').count()===1);
    assert(await page.locator('#company-rate-company .company-detail-body').count()===1);
    assert(await page.locator('#company-rate-company .company-detail-more').count()===0);
    assert.equal(await page.locator('#company-rate-company .drow > td').getAttribute('colspan'),
      String(await page.locator('#company-rate-company .sales-rate-table thead th').count()),'single-container detail spans the full table');
    assert((await page.locator('#company-rate-company .company-detail-header').innerText()).includes('周一(EF4) · 周四(EL2)'));
    assert(await page.locator('#company-rate-realtime .company-detail-header').count()===1);
    assert(await page.locator('#company-rate-realtime .company-detail-body .company-detail-section').count()>=1);
    assert((await page.locator('#company-rate-realtime .company-detail-body').innerText()).includes('燃油附加费'));
    assert((await page.locator('#company-rate-realtime .company-detail-body').innerText()).includes('其他费用'));
    for(const width of [1440,1180,1024,390]){
      await page.setViewportSize({width,height:1000});
      const layout=await page.evaluate(()=>{
        const table=document.querySelector('.realtime-rate-table');
        const price=table.querySelector('.qrow .realtime-ocean-price');
        const priceRect=price.getBoundingClientRect();
        const priceRange=document.createRange();priceRange.selectNodeContents(price);
        const company=document.querySelector('#company-rate-company .company-detail');
        const section=company.querySelector('.company-detail-body');
        const companyRect=company.getBoundingClientRect();
        const companyRowRect=company.closest('tr').previousElementSibling.getBoundingClientRect();
        const checkbox=table.querySelector('.qrow .quote-pick').getBoundingClientRect();
        const row=table.querySelector('.qrow').getBoundingClientRect();
        const columns=Array.from(table.querySelectorAll('thead th'),th=>Math.round(th.getBoundingClientRect().width));
        return {
          display:getComputedStyle(table.querySelector('.qrow')).display,
          priceWidth:priceRect.width,priceTextRight:priceRange.getBoundingClientRect().right,priceRight:priceRect.right,
          priceVisible:priceRect.width>0 && getComputedStyle(price).visibility==='visible',
          companyOverflow:company.scrollWidth>company.clientWidth+1,
          sectionOverflow:section.scrollWidth>section.clientWidth+1,
          companyVisible:section.getBoundingClientRect().width>0,
          companyLeft:companyRect.left,companyRight:companyRect.right,
          companyRowLeft:companyRowRect.left,companyRowRight:companyRowRect.right,
          columns,
          checkboxOffset:checkbox.left-row.left,
          pageOverflow:document.documentElement.scrollWidth>innerWidth+1
        };
      });
      assert(layout.priceVisible,`ocean price hidden at ${width}px`);
      assert(layout.companyVisible,`company detail hidden at ${width}px`);
      if(width===1440) assert(layout.columns[7]>=Math.max(layout.columns[1],layout.columns[2],layout.columns[3]),`ocean price column loses width to carrier/ports: ${JSON.stringify(layout.columns)}`);
      assert(layout.priceTextRight<=layout.priceRight+2,`ocean price clipped at ${width}px: ${JSON.stringify(layout)}`);
      assert(!layout.companyOverflow&&!layout.sectionOverflow,`company detail overflows at ${width}px: ${JSON.stringify(layout)}`);
      assert(Math.abs(layout.companyLeft-layout.companyRowLeft)<32 && Math.abs(layout.companyRowRight-layout.companyRight)<32,
        `company detail does not align with its row at ${width}px: ${JSON.stringify(layout)}`);
      if(width<=1024) assert.equal(layout.display,'grid',`live rate rows should stack at ${width}px`);
      if(width<=1180) assert(layout.checkboxOffset<55,`live rate selection drifts from the left edge at ${width}px: ${JSON.stringify(layout)}`);
      if(width===390) assert(!layout.pageOverflow,`page overflows at ${width}px`);
      await page.screenshot({path:`.work/company-layout-${width}.png`,fullPage:true});
      if(width===1440) {
        await page.locator('#company-rate-company .company-detail').screenshot({path:'.work/company-detail-unified.png'});
        await page.locator('#company-rate-realtime .company-detail').screenshot({path:'.work/realtime-detail-unified.png'});
      }
    }
    await page.locator('#company-rate-company .company-detail-close').click();
    assert(await page.locator('#company-rate-company .drow').isHidden(),'header close control collapses the product detail');
    assert.equal(await page.locator('#company-rate-company .sales-disclosure').getAttribute('aria-expanded'),'false');
    await page.locator('#company-rate-realtime .company-detail-close').click();
    assert(await page.locator('#company-rate-realtime .drow').isHidden(),'online carrier header closes its detail');
    console.log('PASS: company detail and ocean price remain readable at 1440, 1180, 1024 and 390px.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
