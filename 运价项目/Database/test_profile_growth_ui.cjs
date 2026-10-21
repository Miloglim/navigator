const assert=require('node:assert/strict');
const fs=require('node:fs');
const html=fs.readFileSync(__dirname+'/web/index.html','utf8');

assert.doesNotMatch(html,/id="growth-panel"|growth-liquid-toggle|growthLiquidChanged|profile-avatar-edit/);
assert.doesNotMatch(html,/>我的成长</);
assert.match(html,/class="profile-avatar"[^>]+onclick="openProfileAvatarPicker\(\)"/);
assert.match(html,/class="profile-avatar-hover">更换头像/);
assert.doesNotMatch(html,/account-avatar|syncAccountAvatar/);
assert.match(html,/id="profile-growth-compact"/);
assert.match(html,/profile-growth-line \{[^}]*display:flex[^}]*flex-direction:column[^}]*align-items:center/);
assert.match(html,/<div class="profile-growth-line \x27\+growthCycleClass\(s\.cycle\)\+\x27"><div class="profile-growth-rank">[\s\S]*<div class="profile-growth-track"><div class="growth-meter"/);
assert.match(html,/profile-layout \{[^}]*grid-template-columns:minmax\(0,1fr\) 270px[^}]*grid-template-areas:"main identity"/);
assert.match(html,/<aside class="profile-identity-panel">[\s\S]*<div class="profile-name-line"><h2>\$\{esc\(p\.display_name\)\}<\/h2><span class="profile-badge[\s\S]*<\/span><\/div>[\s\S]*<div class="profile-growth-compact/);
assert.match(html,/<div class="profile-main"><form id="profile-form"/);
assert.match(html,/<dl class="profile-technical"><div><dt>账号编号<\/dt><dd>\$\{esc\(p\.user_id\)\}<\/dd><\/div><div><dt>创建时间<\/dt>/);
assert.doesNotMatch(html,/<footer class="profile-technical">/);
assert.match(html,/class="profile-growth-level"/);
assert.match(html,/class="profile-growth-cycle \x27\+\(s\.cycle>1\?/);
assert.match(html,/profile-growth-cycle \x27\+\(s\.cycle>1\?\x27\x27:\x27is-first-cycle\x27\)\+\x27"><span class="profile-growth-level-unit"><strong class="profile-growth-level">/);
assert.match(html,/class="profile-growth-cycle-mark"><span class="profile-growth-diamond" aria-hidden="true"><\/span><b>/);
assert.match(html,/const cycleMark=s\.cycle>1\?/);
assert.match(html,/s\.cycle>1\?\x27\x27:\x27is-first-cycle\x27/);
assert.match(html,/profile-growth-cycle\.is-first-cycle \{[^}]*grid-template-columns:auto[^}]*column-gap:0/);
assert.doesNotMatch(html,/profile-growth-cycle"><span>Ⅰ<\/span>/);
assert.match(html,/profile-growth-level,.profile-growth-cycle-mark b \{[^}]*color:#fff/);
assert.match(html,/profile-growth-level \{[^}]*font-size:18px[^}]*line-height:18px[^}]*translateX\(4px\)[^}]*scaleY\(\.94\)[^}]*tabular-nums/);
assert.match(html,/profile-growth-cycle-mark b \{[^}]*font-size:14px[^}]*line-height:16px[^}]*scaleY\(\.9\)/);
assert.match(html,/profile-growth-level-unit,.profile-growth-cycle-mark \{[^}]*align-items:center[^}]*height:18px/);
assert.match(html,/profile-growth-cycle \{[^}]*column-gap:12px/);
assert.match(html,/profile-growth-cycle-mark \{ gap:3px; \}/);
assert.match(html,/profile-growth-diamond \{[^}]+background:#fff/);
assert.match(html,/growth-cycle-1 \{ --growth-fill:#2563eb; \}/);
assert.match(html,/growth-cycle-3 \{ --growth-fill:linear-gradient\(115deg,#3b82f6,#7c3aed\); \}/);
assert.match(html,/growth-cycle-8 \{[^}]*#a85d00[^}]*#ffd363[^}]*--growth-edge:#ffe29a[^}]*--growth-shadow:/);
assert.match(html,/profile-growth-cycle \{[^}]*border:1px solid var\(--growth-edge\)[^}]*background:var\(--growth-fill\)[^}]*box-shadow:var\(--growth-shadow\)/);
assert.match(html,/function growthCycleClass\(value\) \{ return 'growth-cycle-'\+Math\.min\(8,Math\.max\(1,Number\(value\)\|\|1\)\); \}/);
assert.match(html,/profile-growth-line '\+growthCycleClass\(s\.cycle\)/);
assert.match(html,/role="progressbar" aria-label="距离下一级的经验进度"/);
assert.match(html,/本级 '\+s\.level_progress\+' \/ '\+s\.level_cost\+' XP/);
assert.match(html,/profile-growth-caption \{[^}]*font-size:11px[^}]*line-height:16px[^}]*letter-spacing:0/);
assert.match(html,/growth_liquid_enabled:true/);
assert.match(html,/id="growth-feedback"[^>]+onclick="switchRole\('account'\)"/);
assert.match(html,/function showGrowthFeedback\(award,label\)/);
assert.match(html,/award\.level_up\?'等级提升至 '/);
assert.match(html,/今日 '\+award\.today_xp\+' \/ '\+award\.daily_cap\+' XP/);
assert.match(html,/showGrowthFeedback\(d\.data\.growth_award,'有效查询'\)/);
assert.match(html,/showGrowthFeedback\(r\.growth_award,'审核贡献'\)/);
assert.match(html,/showGrowthFeedback\(r\.growth_award,'候选治理'\)/);
assert.match(html,/@media\(prefers-reduced-motion:reduce\)/);

console.log('PASS: compact account growth and verified XP feedback');













