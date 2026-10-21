const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/web/index.html','utf8');
const source=html.match(/async function loadGroupStats\([^]*?\n}/)[0];
const elements={};const $=id=>elements[id]||=( {innerHTML:'',textContent:'',value:''} );
const groups=[{group:'mixed',total:5,clean:1,reviewed_total:3,clean_rate:33.3,failed_rate:20,reject_rate:20,avg_edits:0.2,awaiting:2,modified_approved:1,mechanical_passed:4,rejected:1},
{group:'unreviewed',total:1,clean:0,reviewed_total:0,clean_rate:null,failed_rate:0,reject_rate:0,avg_edits:0,awaiting:1,modified_approved:0,mechanical_passed:1,rejected:0}];
const ctx=vm.createContext({opsUrl:url=>url,$,canView:()=>true,esc:String,fetch:async()=>({json:async()=>({ok:true,groups})})});
vm.runInContext(source,ctx);
(async()=>{await ctx.loadGroupStats();const output=$('pd-group-stats').innerHTML;
assert.ok(output.includes('33.3%'));assert.ok(output.includes('暂无已审样本'));assert.ok(!output.includes('null%'));
assert.ok(output.includes('未审 2'));assert.ok(output.includes('修改后放行 1'));assert.ok(output.includes('机械通过 4'));
assert.ok(output.includes('未审不计入'));
console.log('PASS: actual group stats renderer shows reviewed denominator, separate counts and no-sample state');
})().catch(error=>{console.error(error);process.exitCode=1;});
