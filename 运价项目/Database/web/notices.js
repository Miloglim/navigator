/* Browser-native editor; server sanitizes every published HTML payload. */
const NOTICE_UI = {rows:[], reviewRows:[], groups:[], routes:[], tags:[], page:1, total:0, sequence:0, editing:null,
  composeScope:{groups:[],routes:[]}, savedRange:null, busy:false, imageBusy:0, user:null, accountRevision:0, expirySupported:false, searchActive:false, filters:{groups:[],routes:[],tag:'',q:'',pending:false}};
const NOTICE_UI_PAGE_SIZE = 20;
const NOTICE_UI_IMAGE_MAX = 2 * 1024 * 1024;
const NOTICE_UI_IMAGE_COUNT = 8;
const NOTICE_EXPIRY_DAYS = [1,3,5,7];
const NOTICE_DAY_MS = 86_400_000;
const NOTICE_REVIEW_LIMIT = 50;
const NOTICE_DEMO_COUNT = Math.max(0, Math.min(50, Number(new URLSearchParams(location.search).get('notice_demo')) || 0));
const NOTICE_DEMO_MODE = NOTICE_DEMO_COUNT > 0;
const ne = id => document.getElementById(id);
const nh = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const noticeAsset = path => (globalThis.NOTICE_ASSET_BASE||'/assets/')+path;
const noticeIcon = name => '<svg class="notice-icon" aria-hidden="true"><use href="'+noticeAsset('notice-icons.svg')+'#'+name+'"></use></svg>';

function noticeSetMode(enabled) {
  document.body.classList.toggle('notice-mode', enabled);
  document.body.classList.toggle('notice-demo', enabled && NOTICE_DEMO_MODE && S.auth?.role === 'admin');
  if (!enabled) return;
  noticeEnsureComposer();
  if (NOTICE_UI.user !== S.auth?.user_id) {
    NOTICE_UI.accountRevision++;
    NOTICE_UI.user = S.auth?.user_id;
    noticeReset();
    NOTICE_UI.expirySupported=false;
    ne('notice-expiry').hidden=true;
    NOTICE_UI.filters = {groups:[],routes:[],tag:'',q:'',pending:false}; NOTICE_UI.searchActive=false; NOTICE_UI.page=1;
  }
}

async function noticeApi(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || '公告请求失败');
  return result.data;
}

function noticeOptions(values, selected, label) {
  return '<option value="">'+nh(label)+'</option>'+values.map(v=>'<option value="'+nh(v)+'"'+(v===selected?' selected':'')+'>'+nh(v)+'</option>').join('');
}

function noticeScopeHtml(context, dimension, values, selected) {
  const label=dimension==='groups'?'群聊':'航线';
  const empty=context==='compose'?'未选':'全部';
  const options=[...new Set([...values,...selected])];
  return '<details class="notice-scope" data-scope-context="'+context+'" data-scope-dimension="'+dimension+'"><summary title="'+nh(selected.join(' · '))+'">'
    +label+'<span class="notice-scope-summary">'+(selected.length?'已选 '+selected.length:empty)+'</span><span aria-hidden="true">▾</span></summary>'
    +'<div class="notice-scope-menu"><input type="search" aria-label="搜索'+label+'" placeholder="搜索'+label+'" oninput="noticeScopeSearch(this)">'
    +'<div class="notice-scope-options">'+options.map(value=>'<label><input type="checkbox" value="'+nh(value)+'" onchange="noticeScopeChange(this)"'+(selected.includes(value)?' checked':'')+'><span>'+nh(value)+'</span></label>').join('')
    +(!options.length?'<span class="notice-scope-empty">暂无可选'+label+'</span>':'')+'</div>'
    +'<button type="button" onclick="noticeScopeClear(this)">清空选择</button></div></details>';
}

function noticeScopeSearch(input) {
  const query=input.value.trim().toLocaleLowerCase();
  input.parentElement.querySelectorAll('.notice-scope-options label').forEach(label=>label.hidden=!label.textContent.toLocaleLowerCase().includes(query));
}

function noticeScopeChange(input) {
  const host=input.closest('.notice-scope');
  const state=host.dataset.scopeContext==='compose'?NOTICE_UI.composeScope:NOTICE_UI.filters;
  state[host.dataset.scopeDimension]=Array.from(host.querySelectorAll('input[type="checkbox"]:checked'),item=>item.value);
  const selected=state[host.dataset.scopeDimension];
  host.querySelector('.notice-scope-summary').textContent=selected.length?'已选 '+selected.length:(host.dataset.scopeContext==='compose'?'未选':'全部');
  host.querySelector('summary').title=selected.join(' · ');
  if(host.dataset.scopeContext==='compose') noticeComposeScopeCaption();
}

function noticeScopeClear(button) {
  const host=button.closest('.notice-scope');
  if(host.dataset.scopeContext==='compose'&&NOTICE_UI.busy) return;
  host.querySelectorAll('input[type="checkbox"]').forEach(input=>input.checked=false);
  noticeScopeChange(button);
}

function noticeRenderComposeScope() {
  const host=ne('notice-compose-scope');if(!host) return;
  host.innerHTML=noticeScopeHtml('compose','routes',NOTICE_UI.routes,NOTICE_UI.composeScope.routes)
    +noticeScopeHtml('compose','groups',NOTICE_UI.groups,NOTICE_UI.composeScope.groups);
  noticeComposeScopeCaption();
}

function noticeComposeScopeCaption() {
  const label=ne('notice-compose-scope-label');if(!label) return;
  const count=NOTICE_UI.composeScope.routes.length+NOTICE_UI.composeScope.groups.length;
  label.textContent='发布范围 · '+(count?'已选 '+count+' 项':'全体');
}

function noticeScopeLabel(row) {
  const routes=row.target_routes||[],groups=row.target_groups||(row.group_name?[row.group_name]:[]);
  return [...routes.map(value=>'航线 '+value),...groups.map(value=>'群聊 '+value)].join(' · ')||'全体';
}

function noticeScopeSummary(row) {
  const routes=row.target_routes||[],groups=row.target_groups||(row.group_name?[row.group_name]:[]);
  if(!routes.length&&!groups.length) return '全体';
  if(routes.length+groups.length>2) return (routes.length?'航线 '+routes.length:'')+(routes.length&&groups.length?' · ':'')+(groups.length?'群聊 '+groups.length:'');
  return noticeScopeLabel(row);
}

function noticeDemoImage(kind, index) {
  const sizes = {
    landscape: [960, 300],
    portrait: [420, 620],
    tall: [520, 820]
  }[kind];
  if (!sizes) return '';
  const [width, height] = sizes;
  const accent = ['#1b5d89', '#a36b18', '#a6423c'][index % 3];
  const title = kind === 'landscape' ? '横向公告图' : kind === 'portrait' ? '竖向公告图' : '长图公告';
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="'+width+'" height="'+height+'" viewBox="0 0 '+width+' '+height+'">'
    +'<rect width="100%" height="100%" fill="#e8eef2"/><rect x="28" y="28" width="'+(width-56)+'" height="'+(height-56)+'" fill="#f8fafb" stroke="#9eb3c1"/>'
    +'<rect x="60" y="62" width="'+Math.max(120,width-120)+'" height="18" fill="'+accent+'"/><rect x="60" y="102" width="'+Math.max(90,width-210)+'" height="10" fill="#718094"/>'
    +'<rect x="60" y="'+Math.round(height*.44)+'" width="'+Math.max(100,width-120)+'" height="8" fill="#9eb3c1"/><rect x="60" y="'+Math.round(height*.57)+'" width="'+Math.max(80,width-190)+'" height="8" fill="#c1cdd5"/>'
    +'<rect x="60" y="'+Math.round(height*.70)+'" width="'+Math.max(70,width-260)+'" height="8" fill="#c1cdd5"/><text x="60" y="'+Math.round(height*.88)+'" font-family="Arial,sans-serif" font-size="22" fill="#17324e">'+title+' · '+String(index+1).padStart(2,'0')+'</text></svg>';
  return '<img src="data:image/svg+xml;charset=UTF-8,'+encodeURIComponent(svg)+'" alt="'+title+'">';
}

function noticeDemoResult(filters,page) {
  const groups=['演示·欧洲价格群','演示·拉美价格群','演示·非洲价格群','演示·东南亚价格群','演示·综合运价群'];
  const routes=['欧洲线','南美东','西非线','东南亚线'];
  const tags=['全体','单群','多群','单航线','群聊与航线','多群与多航线'];
  const countPerType=Math.ceil(NOTICE_DEMO_COUNT/tags.length);
  const rows=Array.from({length:tags.length*countPerType},(_,index)=>{
    const type=Math.floor(index/countPerType),slot=index%countPerType;
    const scopes=[
      {groups:[],routes:[]},
      {groups:[groups[slot]],routes:[]},
      {groups:[groups[slot],groups[(slot+1)%groups.length]],routes:[]},
      {groups:[],routes:[routes[slot%routes.length]]},
      {groups:[groups[slot]],routes:[routes[slot%routes.length]]},
      {groups:[groups[slot],groups[(slot+2)%groups.length]],routes:[routes[slot%routes.length],routes[(slot+1)%routes.length]]}
    ][type];
    return {id:'demo-'+String(index+1).padStart(2,'0'),title:tags[type]+'公告 '+String(index+1).padStart(2,'0')+' · 运价与船期更新',
      author_id:'demo-author',author_name:['航线管理','销售支持','运营中心'][index%3],
      created_at:new Date(Date.now()-index*3_600_000).toLocaleString('sv-SE',{timeZone:'Asia/Shanghai'}),
      updated_at:'',revision:1,pin_state:index===0?'approved':'none',expires_on:null,
      target_groups:scopes.groups,target_routes:scopes.routes,tags:[tags[type]],
      body_html:'<p>这是一条用于公告交叉测试的模拟消息（第 '+(index+1)+' 条），不代表真实运价。</p><p>可以测试发布范围、群聊与航线筛选、翻页和展开正文。</p>'
        + (index%4===0 ? noticeDemoImage('landscape',index) : index%4===1 ? noticeDemoImage('portrait',index) : index%4===2 ? noticeDemoImage('tall',index) : '')};
  }).slice(0,NOTICE_DEMO_COUNT);
  const selected=rows.filter(row=>{
    const scopeMatch=(!row.target_groups.length&&!row.target_routes.length)
      ||(!filters.groups.length&&!filters.routes.length)
      ||row.target_groups.some(value=>filters.groups.includes(value))
      ||row.target_routes.some(value=>filters.routes.includes(value));
    const text=(row.title+' '+row.author_name+' '+noticeExcerpt(row.body_html)).toLocaleLowerCase();
    return scopeMatch&&(!filters.tag||row.tags.includes(filters.tag))&&(!filters.q||text.includes(filters.q.toLocaleLowerCase()));
  });
  return {rows:selected.slice((page-1)*NOTICE_UI_PAGE_SIZE,page*NOTICE_UI_PAGE_SIZE),total:selected.length,groups,routes,tags,expiry_supported:false};
}

async function noticeLoad() {
  if (!S.auth || S.role !== 'sales' || currentCatTab() !== 'dyn') return;
  noticeSetMode(true);
  const sequence=++NOTICE_UI.sequence, user=S.auth?.user_id;
  const filters=NOTICE_UI.filters;
  const params=new URLSearchParams({limit:NOTICE_UI_PAGE_SIZE,offset:(NOTICE_UI.page-1)*NOTICE_UI_PAGE_SIZE});
  const preferredGroups=S.profile?.preferences?.groups || [];
  const preferred=!NOTICE_UI.searchActive && S.useDefaults && preferredGroups.length;
  if (preferred) { params.set('display_mode','preferred'); params.set('display_groups',JSON.stringify(preferredGroups)); }
  if (NOTICE_UI.searchActive) {
    for (const key of ['tag','q']) if(filters[key]) params.set(key,filters[key]);
    for (const key of ['groups','routes']) if(filters[key].length) params.set(key,JSON.stringify(filters[key]));
    if(filters.pending) params.set('pending','1');
  }
  const admin=S.auth.role==='admin';
  const demo=NOTICE_DEMO_MODE && admin;
  const [result,reviewResult]=demo
    ? [noticeDemoResult(filters,NOTICE_UI.page),{rows:[]}]
    : await Promise.all([
      noticeApi('/api/notices?'+params),
      admin?noticeApi('/api/notices?pending=1&limit='+NOTICE_REVIEW_LIMIT):Promise.resolve({rows:[]})
    ]);
  if(sequence!==NOTICE_UI.sequence || user!==S.auth?.user_id || S.role!=='sales' || currentCatTab()!=='dyn') return;
  Object.assign(NOTICE_UI,{rows:result.rows,reviewRows:reviewResult.rows||[],groups:result.groups,routes:result.routes||[],tags:result.tags,total:result.total});
  NOTICE_UI.expirySupported=result.expiry_supported===true;
  ne('notice-expiry').hidden=!NOTICE_UI.expirySupported;
  const filterHtml='<form class="notice-filters" onsubmit="noticeFilter(event)">'
    +noticeScopeHtml('filter','routes',NOTICE_UI.routes,filters.routes)
    +noticeScopeHtml('filter','groups',NOTICE_UI.groups,filters.groups)
    +'<label>标签<select id="notice-tag-filter" onchange="noticeFilter(event)">'+noticeOptions(result.tags,filters.tag,'全部标签')+'</select></label>'
    +'<label class="notice-search">搜索<input type="search" id="notice-search" value="'+nh(filters.q)+'" placeholder="搜索公告中的任意内容"></label>'
    +(admin?'<label class="notice-check"><input id="notice-pending-filter" type="checkbox" onchange="noticeFilter(event)"'+(filters.pending?' checked':'')+'>置顶待审核</label>':'')
    +'<button class="notice-tool" type="submit" title="应用筛选并搜索" aria-label="应用筛选并搜索">'+noticeIcon('Search')+'</button>'
    +'<button class="notice-tool" type="button" onclick="noticeLoad().catch(noticeError)" title="刷新公告" aria-label="刷新公告">'+noticeIcon('RefreshCw')+'</button>'
    +'<span class="notice-filter-count">'+result.total+' 条消息</span></form>';
  const displayBar=demo
    ? '<div class="notice-display-state notice-demo-state">演示模式：'+NOTICE_DEMO_COUNT+' 条模拟公告 · 不写入正式库 <a href="/">退出演示</a></div>'
    : NOTICE_UI.searchActive
    ? '<div class="notice-display-state">本次筛选及全体公告 <button type="button" onclick="noticeClearSearch()">清除查询</button></div>'
    : preferredGroups.length ? '<div class="notice-display-state">'+(preferred?'默认展示：'+nh(preferredGroups.join(' · '))+'及全体公告':'显示全部公告')
      +' <button type="button" onclick="noticeToggleDefault()">'+(preferred?'查看全部':'恢复默认展示')+'</button></div>' : '';
  ne('table-container').innerHTML=noticeBoardHtml(result.rows,result.total,filterHtml,displayBar);
  const pages=Math.max(1,Math.ceil(result.total/NOTICE_UI_PAGE_SIZE));
  if (NOTICE_UI.page > pages) { NOTICE_UI.page=pages; return noticeLoad(); }
  ne('pager').innerHTML='<div class="notice-page"><button class="notice-tool" onclick="noticePage(-1)" title="上一页"'+(NOTICE_UI.page<=1?' disabled':'')+'>'+noticeIcon('ArrowLeft')+'</button><span>'+NOTICE_UI.page+' / '+pages+'</span><button class="notice-tool" onclick="noticePage(1)" title="下一页"'+(NOTICE_UI.page>=pages?' disabled':'')+'>'+noticeIcon('ArrowRight')+'</button></div>';
  noticeRenderComposeScope();
  noticeBindImages(ne('table-container'));
  noticeHighlightMatches(ne('table-container').querySelector('.notice-list'),filters.q);
  noticeRenderReviewQueue(NOTICE_UI.reviewRows);
  noticeScheduleAlignment();
}

function noticeHighlightMatches(root,query) {
  const word=String(query||'').trim();
  if(!root||!word) return;
  const lowerWord=word.toLocaleLowerCase();
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode(node){
    const parent=node.parentElement;
    if(!parent||parent.closest('mark.notice-match,script,style')) return NodeFilter.FILTER_REJECT;
    return node.data.toLocaleLowerCase().includes(lowerWord)?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT;
  }});
  const nodes=[];while(walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(node=>{
    const text=node.data,lower=text.toLocaleLowerCase(),fragment=document.createDocumentFragment();
    let start=0,index=lower.indexOf(lowerWord);
    while(index>=0) {
      fragment.append(text.slice(start,index));
      const mark=document.createElement('mark');mark.className='notice-match';mark.textContent=text.slice(index,index+word.length);fragment.append(mark);
      start=index+word.length;index=lower.indexOf(lowerWord,start);
    }
    fragment.append(text.slice(start));node.replaceWith(fragment);
  });
}

function noticeFitViewport() {
  const paper=document.querySelector('.notice-paper');
  const frame=paper?.closest('.notice-board-frame');
  if(!paper) {
    frame?.style.removeProperty('--notice-paper-max-height');
    return;
  }
  const pager=ne('pager');
  const pagerHeight=Math.ceil(pager?.getBoundingClientRect().height||0);
  const available=Math.max(0,Math.floor(innerHeight-paper.getBoundingClientRect().top-pagerHeight));
  frame.style.setProperty('--notice-paper-max-height',available+'px');
}

function noticeScheduleAlignment() {
  cancelAnimationFrame(noticeScheduleAlignment.frame||0);
  noticeScheduleAlignment.frame=requestAnimationFrame(()=>requestAnimationFrame(()=>{
    noticeFitViewport();
  }));
  if(!noticeScheduleAlignment.bound) {
    addEventListener('resize',noticeScheduleAlignment,{passive:true});
    noticeScheduleAlignment.bound=true;
  }
}

function noticeBoardHtml(rows,total,filters,displayBar='') {
  const parts=Object.fromEntries(new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',weekday:'long'}).formatToParts(new Date()).map(part=>[part.type,part.value]));
  const date=parts.year+'.'+parts.month+'.'+parts.day+' · '+parts.weekday;
  const lead=rows[0], briefs=rows.slice(1,4), secondary=rows.slice(4,6), remaining=rows.slice(6);
  const stories=lead?'<div class="notice-list"><section class="notice-feature-row'+(briefs.length?'':' notice-feature-solo')+'" aria-label="公告列表">'
    +noticePostHtml(lead,'lead')
    +(briefs.length?'<aside class="notice-rail"><div class="notice-rail-heading">公告速览</div>'+briefs.map(row=>noticePostHtml(row,'brief')).join('')+'</aside>':'')
    +(secondary.length?'<div class="notice-feature-secondary">'+secondary.map(row=>noticePostHtml(row,'standard')).join('')+'</div>':'')
    +'</section>'
    +(remaining.length?'<section class="notice-more"><div class="notice-section-heading">更多公告 <span>'+remaining.length+' 条</span></div><div class="notice-below">'
      +remaining.map(row=>noticePostHtml(row,'standard')).join('')+'</div></section>':'')+'</div>'
    :'<div class="empty-state"><p>暂无公告</p></div>';
  const paper='<div class="notice-paper"><div class="notice-mast"><h1>团队公告</h1><span>'+total+' 条消息 · '+nh(date)+'</span></div>'+displayBar+filters+stories+'</div>';
  return '<div class="notice-board-frame">'+paper+'</div>';
}

function noticePostHtml(row,placement='standard') {
  const mine=row.author_id===S.auth.user_id && !NOTICE_DEMO_MODE, admin=S.auth.role==='admin' && !NOTICE_DEMO_MODE;
  const createdAt=String(row.created_at||'').replace(/:\d{2}$/,'');
  const actionIcons={edit:'Pencil',delete:'Trash2',request:'Pin',unpin:'PinOff',approve:'Check',reject:'X'};
  const action=(kind,label)=>'<button type="button" data-action="'+kind+'" onclick="noticeAction(\''+row.id+'\',\''+kind+'\')" title="'+nh(label)+'" aria-label="'+nh(label)+'">'+noticeIcon(actionIcons[kind]||'Ellipsis')+'</button>';
  let actions='';
  if(mine||admin) {
    actions+=action('edit','编辑')+action('delete','删除');
    actions+=row.pin_state==='approved'||row.pin_state==='pending' ? action('unpin',row.pin_state==='pending'?'撤回申请':'取消置顶') : action('request',S.auth.role==='sales'?'申请置顶':'置顶');
  }
  if(admin&&row.pin_state==='pending') actions+=action('approve','批准置顶')+action('reject','驳回');
  const state=row.pin_state==='approved'?'<span class="notice-label pin">置顶</span>':row.pin_state==='pending'?'<span class="notice-label pending">置顶待审核</span>':row.pin_state==='rejected'&&(mine||admin)?'<span class="notice-label">置顶未通过</span>':'';
  const marker=placement==='lead'&&row.pin_state==='approved'?'<div class="notice-story-marker is-pinned">置顶公告</div>':'';
  const avatar=/^av-(0[1-9]|[12]\d|30)$/.test(row.avatar_id||'')
    ? '<span class="notice-avatar"><img src="'+noticeAsset('avatars/'+row.avatar_id+'.png')+'" alt="" loading="lazy"></span>'
    : '<span class="notice-avatar notice-avatar-fallback">'+nh(Array.from(row.author_name)[0]||'人')+'</span>';
  return '<article class="notice-post notice-post-'+placement+'" data-notice-id="'+row.id+'">'+marker+'<header><div class="notice-author-row">'+avatar
    +'<div class="notice-byline"><span class="notice-author-main"><b>'+nh(row.author_name)+'</b><time>'+nh(createdAt)+'</time></span>'
    +(row.revision>1?'<span title="'+nh(row.updated_at)+'">已更新</span>':'')+state
    +'<span class="notice-label notice-targets" title="'+nh(noticeScopeLabel(row))+'">'+nh(noticeScopeSummary(row))+'</span>'
    +(row.expires_on?'<span class="notice-label expiry">有效至 '+nh(row.expires_on)+'</span>':'')
    +'</div>'+(actions?'<div class="notice-post-actions">'+actions+'</div>':'')+'</div></header>'
    +'<div class="notice-story"><h2>'+nh(row.title)+'</h2></div>'
    +'<div class="notice-content">'+row.body_html+'</div>'
    +'<div class="notice-tags">'+row.tags.map(t=>'<button onclick="noticeChooseTag(this.dataset.tag)" data-tag="'+nh(t)+'">#'+nh(t)+'</button>').join('')+'</div></article>';
}

function noticeFirstImage(html) {
  const root=document.createElement('div');root.innerHTML=html;
  return root.querySelector('img')?.getAttribute('src')||'';
}

function noticeExcerpt(html) {
  const root=document.createElement('div');root.innerHTML=html;
  root.querySelectorAll('img').forEach(image=>image.remove());
  root.querySelectorAll('p,div,li,br').forEach(block=>block.after(document.createTextNode(' ')));
  return root.textContent.replace(/\s+/g,' ').trim();
}

function noticeError(error) { toast(error.message || String(error),'error'); }
function noticeFilter(event) {
  event?.preventDefault();
  Object.assign(NOTICE_UI.filters,{tag:ne('notice-tag-filter').value,q:ne('notice-search').value.trim(),pending:!!ne('notice-pending-filter')?.checked});
  NOTICE_UI.searchActive=true; NOTICE_UI.page=1; noticeLoad().catch(noticeError);
}
function noticeChooseGroup(group) { NOTICE_UI.filters.groups=[group];NOTICE_UI.filters.routes=[];NOTICE_UI.searchActive=true;NOTICE_UI.page=1;noticeLoad().catch(noticeError); }
function noticeChooseTag(tag) { NOTICE_UI.filters.tag=tag;NOTICE_UI.searchActive=true;NOTICE_UI.page=1;noticeLoad().catch(noticeError); }
function noticeClearSearch() { NOTICE_UI.filters={groups:[],routes:[],tag:'',q:'',pending:false};NOTICE_UI.searchActive=false;NOTICE_UI.page=1;S.useDefaults=true;renderPersonalScope();noticeLoad().catch(noticeError); }
function noticeToggleDefault() { S.useDefaults=!S.useDefaults;NOTICE_UI.page=1;renderPersonalScope();noticeLoad().catch(noticeError); }
function noticePage(change) { NOTICE_UI.page+=change;noticeLoad().catch(noticeError); }

function noticeBindImages(root) {
  root?.querySelectorAll('.notice-content img,.notice-lead-media img').forEach(img=>{
    if(img.dataset.noticePreviewBound==='1') return;
    img.dataset.noticePreviewBound='1';
    img.tabIndex=0;img.setAttribute('role','button');
    img.setAttribute('aria-label',img.alt?'查看图片：'+img.alt:'查看公告图片');
    img.addEventListener('click',()=>noticePreview(img.src,img.alt));
    img.addEventListener('keydown',event=>{
      if(event.key==='Enter'||event.key===' ') {event.preventDefault();noticePreview(img.src,img.alt);}
    });
  });
}

function noticeToggleReviewQueue() {
  const panel=ne('notice-review-panel'),toggle=ne('notice-review-toggle');
  if(!panel||!toggle) return;
  panel.hidden=!panel.hidden;
  toggle.setAttribute('aria-expanded',String(!panel.hidden));
}

function noticeReviewAction(button,action) {
  const id=button.closest('[data-notice-id]')?.dataset.noticeId;
  if(id) noticeAction(id,action);
}

function noticeRenderReviewQueue(rows) {
  noticeEnsureComposer();
  const host=ne('notice-review'),panel=ne('notice-review-panel'),toggle=ne('notice-review-toggle');
  const visible=S.auth?.role==='admin'&&rows.length>0;
  host.hidden=!visible;
  if(!visible) {panel.hidden=true;toggle.setAttribute('aria-expanded','false');panel.innerHTML='';return;}
  ne('notice-review-count').textContent=rows.length;
  panel.innerHTML='<header><strong>置顶申请</strong><span>'+rows.length+' 条待处理</span></header><div class="notice-review-list">'
    +rows.map(row=>'<article class="notice-review-item" data-notice-id="'+nh(row.id)+'"><div><strong>'+nh(row.title)+'</strong><span>'+nh(row.author_name)+' · '+nh(noticeScopeLabel(row))+'</span></div>'
      +'<div class="notice-review-actions"><button type="button" onclick="noticeReviewAction(this,&quot;approve&quot;)" title="批准置顶" aria-label="批准置顶">'+noticeIcon('Check')+'</button>'
      +'<button type="button" onclick="noticeReviewAction(this,&quot;reject&quot;)" title="驳回置顶" aria-label="驳回置顶">'+noticeIcon('X')+'</button></div></article>').join('')
    +'</div>';
}

function noticeEnsureComposer() {
  if(ne('notice-composer')) return;
  document.addEventListener('click',event=>{
    document.querySelectorAll('.notice-scope[open]').forEach(menu=>{
      if(!menu.contains(event.target)) menu.open=false;
    });
  });
  document.addEventListener('keydown',event=>{
    if(event.key!=='Escape') return;
    const menus=document.querySelectorAll('.notice-scope[open]');
    if(menus.length) {event.preventDefault();menus.forEach(menu=>menu.open=false);menus[0].querySelector('summary').focus();}
  });
  const tools=[['undo','Undo2','撤销'],['redo','Redo2','重做'],['bold','Bold','加粗'],['italic','Italic','斜体'],['underline','Underline','下划线'],['strikeThrough','Strikethrough','删除线'],['insertUnorderedList','List','项目列表'],['insertOrderedList','ListOrdered','编号列表'],['justifyLeft','AlignLeft','左对齐'],['justifyCenter','AlignCenter','居中'],['justifyRight','AlignRight','右对齐'],['removeFormat','RemoveFormatting','清除格式']];
  document.body.insertAdjacentHTML('beforeend','<aside class="notice-review" id="notice-review" hidden><section class="notice-review-panel" id="notice-review-panel" hidden></section>'
    +'<button type="button" class="notice-review-toggle" id="notice-review-toggle" onclick="noticeToggleReviewQueue()" aria-expanded="false" aria-controls="notice-review-panel">'+noticeIcon('Pin')+'置顶申请 <b id="notice-review-count">0</b></button></aside>'
    +'<button id="notice-fab" class="notice-fab" onclick="noticeOpen()" title="发布消息" aria-label="发布消息">'+noticeIcon('Pencil')+'发布消息</button>'
    +'<section class="notice-composer" id="notice-composer" role="dialog" aria-label="发布消息" hidden><header class="notice-compose-head"><strong id="notice-compose-label">发布消息</strong><div>'
    +'<button class="notice-tool" onclick="noticeExpand()" title="展开 / 恢复" aria-label="展开 / 恢复">'+noticeIcon('Maximize2')+'</button><button class="notice-tool" onclick="noticeCollapse()" title="收起，保留草稿" aria-label="收起，保留草稿">'+noticeIcon('Minus')+'</button></div></header>'
    +'<div class="notice-compose-fields"><label class="notice-title">标题<input id="notice-compose-title" maxlength="160"></label>'
    +'<div class="notice-compose-scope-field"><span id="notice-compose-scope-label">发布范围 · 全体</span><div id="notice-compose-scope"></div></div><label>标签<textarea id="notice-compose-tags" placeholder="每行一个标签，按回车分隔" maxlength="240" rows="2"></textarea></label>'
    +'<div class="notice-expiry" id="notice-expiry" hidden><span>有效期</span><div class="notice-expiry-options" role="group" aria-label="公告有效期">'
    +'<button type="button" data-expiry-days="0" aria-pressed="true" onclick="noticeSetExpiry(0)">长期</button>'
    +NOTICE_EXPIRY_DAYS.map(days=>'<button type="button" data-expiry-days="'+days+'" aria-pressed="false" onclick="noticeSetExpiry('+days+')">'+days+'天</button>').join('')
    +'<label>截止日期<input id="notice-expires-on" type="date" aria-label="截止日期" onchange="noticeExpiryChanged()"></label></div></div></div>'
    +'<div class="notice-editor-format" role="group" aria-label="文字设置">'
    +'<select title="段落格式" aria-label="段落格式" onchange="noticeCommand(\'formatBlock\',this.value)"><option value="p">正文</option><option value="h2">标题</option><option value="h3">小标题</option><option value="blockquote">引用</option><option value="pre">预格式</option></select>'
    +'<select title="字号" aria-label="字号" onchange="noticeCommand(\'fontSize\',this.value)"><option value="2">13</option><option value="3" selected>16</option><option value="4">18</option><option value="5">24</option><option value="6">30</option></select>'
    +'</div><div class="notice-editor-toolbar" role="toolbar" aria-label="消息格式工具栏">'
    +tools.map(t=>'<button type="button" class="notice-tool" data-command="'+t[0]+'" title="'+t[2]+'" aria-label="'+t[2]+'">'+noticeIcon(t[1])+'</button>').join('')
    +'<input type="color" value="#167a70" title="文字颜色" aria-label="文字颜色" onchange="noticeCommand(\'foreColor\',this.value)">'
    +'<input type="color" value="#fff2b3" title="高亮颜色" aria-label="高亮颜色" onchange="noticeCommand(\'hiliteColor\',this.value)">'
    +'<button class="notice-tool" onclick="noticeLink()" title="插入链接" aria-label="插入链接">'+noticeIcon('Link')+'</button>'
    +'<button class="notice-tool" onclick="ne(\'notice-image-input\').click()" title="插入图片" aria-label="插入图片">'+noticeIcon('Image')+'</button><input id="notice-image-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden></div>'
    +'<div id="notice-editor" class="notice-editor" contenteditable="true" role="textbox" aria-label="消息正文" aria-multiline="true"></div>'
    +'<footer class="notice-compose-footer"><label><input type="checkbox" id="notice-compose-pin"><span id="notice-pin-label">申请置顶</span></label><button class="notice-tool" onclick="noticeDiscard()">清空</button>'
    +'<span id="notice-compose-status" class="notice-status" role="status"></span><button id="notice-publish" class="notice-submit" onclick="noticePublish()">发布</button></footer></section>'
    +'<dialog class="notice-image-dialog" id="notice-image-dialog"><button class="notice-tool" onclick="ne(\'notice-image-dialog\').close()" aria-label="关闭图片">&times;</button><img alt="公告原图" id="notice-preview-image"></dialog>');
  ne('notice-editor').addEventListener('paste',noticePaste);
  ne('notice-editor').addEventListener('keyup',noticeRemember);
  ne('notice-editor').addEventListener('mouseup',noticeRemember);
  ne('notice-editor').addEventListener('blur',noticeRemember);
  ne('notice-editor').addEventListener('drop',event=>{event.preventDefault();noticeAddFiles(Array.from(event.dataTransfer.files));});
  ne('notice-image-input').addEventListener('change',async event=>{await noticeAddFiles(Array.from(event.target.files));event.target.value='';});
  ne('notice-composer').querySelectorAll('[data-command]').forEach(button=>{
    button.addEventListener('mousedown',event=>event.preventDefault());
    button.addEventListener('click',()=>noticeCommand(button.dataset.command));
  });
  ne('notice-composer').addEventListener('keydown',event=>{if(event.key==='Escape') noticeCollapse();});
}

function noticeOpen() {
  noticeEnsureComposer();
  ne('notice-composer').hidden=false;
  ne('notice-pin-label').textContent=S.auth.role==='sales'?'申请置顶':'置顶';
  noticeRenderComposeScope();
  ne('notice-expires-on').min=noticeToday();
  ne('notice-compose-title').focus();
}
function noticeToday() {
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{year:'numeric',month:'2-digit',day:'2-digit',timeZone:'Asia/Shanghai'}).formatToParts(new Date()).map(part=>[part.type,part.value]));
  return parts.year+'-'+parts.month+'-'+parts.day;
}
function noticeExpiryDate(days) {
  const [year,month,day]=noticeToday().split('-').map(Number);
  return new Date(Date.UTC(year,month-1,day+days-1)).toISOString().slice(0,10);
}
function noticeSetExpiry(days) {
  ne('notice-expires-on').value=days ? noticeExpiryDate(days) : '';
  noticeExpiryChanged();
}
function noticeExpiryChanged() {
  const selected=ne('notice-expires-on').value;
  const days=selected ? Math.round((Date.parse(selected+'T00:00:00Z')-Date.parse(noticeToday()+'T00:00:00Z'))/NOTICE_DAY_MS)+1 : 0;
  ne('notice-composer').querySelectorAll('[data-expiry-days]').forEach(button=>button.setAttribute('aria-pressed',String(Number(button.dataset.expiryDays)===days)));
}
function noticeCollapse() { ne('notice-composer').hidden=true;ne('notice-fab').focus(); }
function noticeExpand() { ne('notice-composer').classList.toggle('expanded'); }
function noticeStatus(message,error=false) { ne('notice-compose-status').textContent=message;ne('notice-compose-status').classList.toggle('error',error); }
function noticeReset() {
  if(!ne('notice-composer')) return;
  NOTICE_UI.editing=null; NOTICE_UI.savedRange=null;
  for(const id of ['notice-compose-title','notice-compose-tags']) ne(id).value='';
  NOTICE_UI.composeScope={groups:[],routes:[]};noticeRenderComposeScope();ne('notice-compose-pin').checked=false;ne('notice-compose-pin').disabled=false;
  ne('notice-expires-on').value='';noticeExpiryChanged();
  ne('notice-editor').innerHTML='';ne('notice-compose-label').textContent='发布消息';ne('notice-publish').textContent='发布';noticeStatus('');
}
function noticeClearAccount() {
  NOTICE_UI.sequence++;
  NOTICE_UI.accountRevision++;
  NOTICE_UI.busy=false; NOTICE_UI.imageBusy=0;
  NOTICE_UI.user=null; NOTICE_UI.rows=[]; NOTICE_UI.reviewRows=[]; NOTICE_UI.groups=[]; NOTICE_UI.routes=[]; NOTICE_UI.tags=[];
  NOTICE_UI.expirySupported=false;
  if(ne('notice-expiry')) ne('notice-expiry').hidden=true;
  noticeReset();
  document.body.classList.remove('notice-mode');
  if(ne('notice-composer')) { noticePublishing(false); ne('notice-composer').hidden=true; }
  if(ne('notice-review')) noticeRenderReviewQueue([]);
  if(ne('notice-image-dialog')?.open) ne('notice-image-dialog').close();
  if(typeof closeLightbox==='function') closeLightbox();
  if(ne('notice-preview-image')) ne('notice-preview-image').removeAttribute('src');
}
function noticeDiscard() {
  if(NOTICE_UI.busy||NOTICE_UI.imageBusy) return;
  if((ne('notice-editor').innerHTML||ne('notice-compose-title').value)&&!confirm('清空当前草稿？')) return;
  noticeReset();
}
function noticeRemember() {
  const selection=window.getSelection();
  if(selection.rangeCount&&ne('notice-editor').contains(selection.anchorNode)) NOTICE_UI.savedRange=selection.getRangeAt(0).cloneRange();
}
function noticeRestore() {
  const editor=ne('notice-editor');editor.focus();
  const selection=window.getSelection();
  if(NOTICE_UI.savedRange&&editor.contains(NOTICE_UI.savedRange.commonAncestorContainer)) {
    selection.removeAllRanges();selection.addRange(NOTICE_UI.savedRange);
  } else { const range=document.createRange();range.selectNodeContents(editor);range.collapse(false);selection.removeAllRanges();selection.addRange(range); }
}
function noticeCommand(command,value=null) { if(NOTICE_UI.busy) return; noticeRestore();document.execCommand(command,false,value);noticeRemember(); }
function noticeLink() {
  const href=prompt('链接地址（http、https 或 mailto）');
  if(!href) return;
  if(!/^(https?:\/\/|mailto:)[^\s]+$/i.test(href.trim())) {noticeStatus('链接地址无效',true);return;}
  noticeCommand('createLink',href.trim());
}

function noticeCleanPaste(html) {
  const doc=new DOMParser().parseFromString(html,'text/html');
  doc.querySelectorAll('script,style,svg,math,iframe,object,embed,template').forEach(node=>node.remove());
  const allowed=new Set(['P','DIV','BR','STRONG','B','EM','I','U','S','STRIKE','UL','OL','LI','BLOCKQUOTE','H2','H3','PRE','SPAN','A','IMG','FONT']);
  doc.body.querySelectorAll('*').forEach(node=>{
    if(!allowed.has(node.tagName)) { node.replaceWith(...node.childNodes);return; }
    for(const attr of Array.from(node.attributes)) if(!['style','href','src','color','size'].includes(attr.name)) node.removeAttribute(attr.name);
    const styles=Array.from(node.style).filter(key=>['color','background-color','text-align','font-size'].includes(key)).map(key=>key+':'+node.style.getPropertyValue(key));
    node.removeAttribute('style');if(styles.length) node.setAttribute('style',styles.join(';'));
    if(node.tagName==='IMG'&&!/^data:image\/(png|jpeg|gif|webp);base64,/i.test(node.getAttribute('src')||'')) node.remove();
    if(node.tagName==='A'&&!/^(https?:\/\/|mailto:)[^\s]+$/i.test(node.getAttribute('href')||'')) node.removeAttribute('href');
  });
  return doc.body.innerHTML;
}
function noticePaste(event) {
  event.preventDefault();noticeRemember();
  const images=Array.from(event.clipboardData.items).filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile());
  if(images.length) {noticeAddFiles(images);return;}
  const html=event.clipboardData.getData('text/html');
  if(html) noticeCommand('insertHTML',noticeCleanPaste(html));
  else noticeCommand('insertText',event.clipboardData.getData('text/plain'));
}
async function noticeAddFiles(files) {
  if(NOTICE_UI.busy) return;
  const user=S.auth?.user_id, generation=NOTICE_UI.accountRevision;
  NOTICE_UI.imageBusy++;ne('notice-publish').disabled=true;
  try {
    for(const file of files) {
      if(!['image/png','image/jpeg','image/gif','image/webp'].includes(file.type)) throw new Error('图片仅支持 PNG、JPEG、GIF、WebP');
      if(file.size>NOTICE_UI_IMAGE_MAX) throw new Error('每张图片不能超过 2 MB');
      if(ne('notice-editor').querySelectorAll('img').length>=NOTICE_UI_IMAGE_COUNT) throw new Error('每条公告最多 8 张图片');
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('图片读取失败'));reader.readAsDataURL(file);});
      if(user!==S.auth?.user_id || generation!==NOTICE_UI.accountRevision) return;
      noticeCommand('insertHTML','<img src="'+data+'" alt="公告图片"><p><br></p>');
    }
    noticeStatus('图片已插入');
  } catch(error) {if(generation===NOTICE_UI.accountRevision) noticeStatus(error.message,true);}
  finally {if(generation===NOTICE_UI.accountRevision) {NOTICE_UI.imageBusy--;ne('notice-publish').disabled=NOTICE_UI.busy||NOTICE_UI.imageBusy>0;}}
}

function noticePublishing(active) {
  NOTICE_UI.busy=active;
  ne('notice-editor').contentEditable=active?'false':'true';
  ne('notice-composer').querySelectorAll('input,select,textarea,[data-command],[data-expiry-days]').forEach(control=>control.disabled=active);
  if(!active) ne('notice-compose-pin').disabled=!!NOTICE_UI.editing;
  ne('notice-publish').disabled=active||NOTICE_UI.imageBusy>0;
}

async function noticePublish() {
  if(NOTICE_UI.busy||NOTICE_UI.imageBusy) return;
  const title=ne('notice-compose-title').value.trim();
  if(!title) {noticeStatus('请填写标题',true);ne('notice-compose-title').focus();return;}
  const editor=ne('notice-editor');
  if(!editor.textContent.trim()&&!editor.querySelector('img')) {noticeStatus('请填写正文或插入图片',true);editor.focus();return;}
  const user=S.auth?.user_id, generation=NOTICE_UI.accountRevision;
  const expiry=ne('notice-expires-on');
  if(expiry.validity.badInput || (expiry.value && expiry.value<noticeToday())) {noticeStatus('有效期不能早于今天',true);expiry.focus();return;}
  const body={title,body_html:editor.innerHTML,target_groups:[...NOTICE_UI.composeScope.groups],target_routes:[...NOTICE_UI.composeScope.routes],tags:ne('notice-compose-tags').value.split(/\r?\n/).map(t=>t.trim()).filter(Boolean),pin:ne('notice-compose-pin').checked};
  if(NOTICE_UI.expirySupported) body.expires_on=expiry.value||null;
  if(NOTICE_UI.editing) Object.assign(body,{id:NOTICE_UI.editing.id,revision:NOTICE_UI.editing.revision});
  noticePublishing(true);noticeStatus('发布中…');
  try {
    const result=await noticeApi('/api/notices/save',body);
    if(user!==S.auth?.user_id || generation!==NOTICE_UI.accountRevision) return;
    noticeReset();noticeCollapse();NOTICE_UI.page=1;
    toast(result.pin_state==='pending'?'已发布，置顶申请等待管理员审核':'公告已保存','success');
    await noticeLoad();
  } catch(error) {if(generation===NOTICE_UI.accountRevision) noticeStatus(error.message,true);}
  finally {if(generation===NOTICE_UI.accountRevision) noticePublishing(false);}
}

async function noticeAction(id,action) {
  const row=[...NOTICE_UI.rows,...NOTICE_UI.reviewRows].find(item=>item.id===id);if(!row) return;
  if(action==='edit') {
    if(NOTICE_UI.busy||NOTICE_UI.imageBusy) return;
    if((ne('notice-compose-title').value||ne('notice-editor').innerHTML)&&!confirm('替换当前草稿，编辑这条公告？')) return;
    noticeReset();NOTICE_UI.editing=row;noticeOpen();
    ne('notice-compose-label').textContent='编辑消息';ne('notice-publish').textContent='保存';
    ne('notice-compose-title').value=row.title;
    NOTICE_UI.composeScope={groups:[...(row.target_groups||(row.group_name?[row.group_name]:[]))],routes:[...(row.target_routes||[])]};noticeRenderComposeScope();
    ne('notice-compose-tags').value=row.tags.join('\n');ne('notice-editor').innerHTML=row.body_html;
    ne('notice-expires-on').value=row.expires_on||'';noticeExpiryChanged();
    ne('notice-compose-pin').checked=['pending','approved'].includes(row.pin_state);ne('notice-compose-pin').disabled=true;
    if(S.auth.role==='sales'&&row.pin_state==='approved') noticeStatus('修改后将重新审核置顶');
    return;
  }
  if(action==='delete'&&!confirm('删除这条公告？')) return;
  try {
    await noticeApi(action==='delete'?'/api/notices/delete':'/api/notices/pin',{id,revision:row.revision,action});
    await noticeLoad();
    if(typeof refreshNotificationCount==='function') refreshNotificationCount();
  } catch(error) {noticeError(error);}
}
function noticePreview(src,alt='') {
  if(typeof openLightbox==='function') {
    openLightbox(src,alt||'公告原图');
    return;
  }
  noticeEnsureComposer();
  ne('notice-preview-image').src=src;
  ne('notice-preview-image').alt=alt||'公告原图';
  ne('notice-image-dialog').showModal();
}
