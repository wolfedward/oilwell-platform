import {renderUsers} from '../../pages/users.js';
import {renderRoles} from '../../pages/roles.js';
import {renderAudit} from '../../pages/audit.js';
import {renderSystemHealth} from '../../pages/system-health.js';
import {renderAcceptanceCenter} from '../../pages/acceptance-center.js';
import {note} from '../ui.js';
const renders={users:renderUsers,roles:renderRoles,audit:renderAudit,'system-health':renderSystemHealth,'acceptance-center':renderAcceptanceCenter};
export async function renderAdministrationPreview(ctx){
 await renders[ctx.page.id](ctx);if(!ctx.isCurrent())return;
 ctx.workspace.querySelector('.page-header')?.insertAdjacentHTML('afterend',note('页面预览独立运行。历史身份已脱敏；账号与权限编辑只演练，不影响当前登录资格。年度内容是页面映射，不是研发完成或现场验收证明。'));
 if(['system-health','acceptance-center'].includes(ctx.page.id))ctx.workspace.querySelector('.page-header')?.insertAdjacentHTML('afterend',note('完整性结果来自本次服务启动时对固定只读快照的校验，不在每次打开页面时重复扫描整库。'));
 if(ctx.page.id==='acceptance-center'){
  const cards=ctx.workspace.querySelectorAll('.metric-card');if(cards[0])cards[0].querySelector('span').textContent='覆盖已确认的三年度技术任务';if(cards[1]){cards[1].querySelector('p').textContent='页面与任务关联';cards[1].querySelector('span').textContent='设计目录关系，非完成成果数量';}
  const text=ctx.workspace.querySelector('.acceptance-note .panel-body');if(text)text.textContent='仅对年度工作任务逐项展示页面入口；不包含专利、奖项等申报绩效。页面、算法软件测试与现场验证必须分别审查，页面数量不代表生产研发完成度。';
 }
 if(ctx.page.id==='system-health'){
  const cards=ctx.workspace.querySelectorAll('.metric-card');if(cards[2]){cards[2].querySelector('p').textContent='设备采集 / 控制';cards[2].querySelector('span').textContent='本预览不启动 MQTT、Modbus 或终端模拟器';}
 }
}
