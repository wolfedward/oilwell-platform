import {escapeHtml as esc} from '../../components.js';
import {studyDetail} from '../../studies/workbench.js';
import {bindStudyCharts} from '../../studies/charts.js';
import {panel,note,links,table,number,metrics} from '../ui.js';
export async function renderEnergyWorkspace(ctx){
 const data=await ctx.api.get('/api/preview/views/energy');if(!ctx.isCurrent())return;
 const schedule=ctx.page.id==='multiwell-schedule';
 ctx.workspace.innerHTML=ctx.pageHeader(ctx.page)+note('软件测试场景，与历史策略报表分开。未接入设备，不构成现场节能验证。')+links([['strategy-center','策略与版本'],['recovery-analysis','产液率恢复'],['recovery-lab','液面恢复研究'],['energy-overview','历史效果报表']])+(schedule?
 panel('多井约束与影子计划',`<form class="preview-filter" data-schedule-form><label>最大同时运行井数<input name="max_concurrent" type="number" min="1" max="20" value="2" required></label><label>日启动次数上限<input name="max_starts" type="number" min="1" max="48" value="16" required></label><label>并发功率上限 kW<input name="max_kw" type="number" min="1" max="1000" value="30" required></label><button class="button primary">保存约束输入</button></form><p data-energy-message></p>`)+panel('固定输入的可行候选',studyDetail(data.schedule))+panel('无可行制度示例',studyDetail(data.rejected)):
 metrics([['对照样例',data.effects.length,'组'],['比较性质','描述性'],['现场验证','未完成'],['设备执行','未启用']])+panel('同长度、不重叠窗口',table([['well','油井'],['baseline_from','基线开始'],['baseline_to','基线结束'],['evaluation_from','比较开始'],['evaluation_to','比较结束'],['conclusion','结论边界']],data.effects))+data.effects.map(r=>panel(r.well,table([['metric','指标'],['before','基线'],['after','比较期']],[{metric:'用电量 kWh',before:number(r.baseline_energy_kwh),after:number(r.after_energy_kwh)},{metric:'产液量 m³',before:number(r.baseline_liquid_m3),after:number(r.after_liquid_m3)},{metric:'覆盖率',before:r.baseline_energy_kwh==null?'不足':'>=95%',after:number(r.coverage*100)+'%'}])+note(r.conclusion))).join(''));
 ctx.registerDispose(bindStudyCharts(ctx.workspace));
 const form=ctx.workspace.querySelector('[data-schedule-form]');if(form)form.onsubmit=async e=>{e.preventDefault();if(!form.reportValidity())return;const saved=await ctx.actions.confirmRehearsal({action:'calculate_input',objectId:'preview-schedule',values:Object.fromEntries(new FormData(form))});if(saved&&ctx.isCurrent())ctx.workspace.querySelector('[data-energy-message]').textContent='约束已保存，尚未计算；下方固定计划未更新。';};
}
