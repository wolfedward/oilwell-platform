from copy import deepcopy
import math
from .studies import records
def fixture():
    faults=[{'id':'preview-M01','name':'三相运行正常','ia':28.3,'ib':28.7,'ic':28.1,'voltage':381.2,'temperature':62.4,'severity':'正常','evidence':'三相 RMS 接近，测试温度稳定'},
    {'id':'preview-M02','name':'缺相测试','ia':1.2,'ib':38.4,'ic':39.1,'voltage':380.7,'temperature':68.1,'severity':'保护锁存','evidence':'A 相电流缺失；需核对 CT 接线与一次回路'},
    {'id':'preview-M03','name':'电流不平衡','ia':21.8,'ib':32.4,'ic':35.1,'voltage':379.8,'temperature':71.2,'severity':'告警','evidence':'相电流最大偏差增大，不能单独判定绕组损坏'},
    {'id':'preview-M04','name':'温升异常','ia':31.2,'ib':31.8,'ic':31.1,'voltage':380.2,'temperature':96.5,'severity':'延时资格','evidence':'测试温度超过整定值，待持续时间确认'}]
    for i,(name,voltage,current,speed,reason) in enumerate([('过压测试',432,29,1470,'供电偏高，核对测量与电源'),('欠压测试',318,37,1410,'供电偏低，核对电压降'),('过流测试',380,48,1455,'负载增加，核对持续时间'),('堵转测试',378,82,0,'转速为零且电流升高，核对机械阻滞')],5):
        faults.append({'id':f'preview-M{i:02d}','name':name,'ia':current,'ib':current*.99,'ic':current*1.01,'voltage':voltage,'temperature':73.2,'speed_rpm':speed,'severity':'告警','evidence':reason})
    for row in faults:
        row.update(sample_at='2026-09-12T02:00:00Z',quality='software_valid',source_kind='software_fixture',speed_rpm=row.get('speed_rpm',1470),power_kw=round(math.sqrt(3)*row['voltage']*(row['ia']+row['ib']+row['ic'])/3*.84/1000,2))
    event_waveform=[{'relative_ms':i*100,'current_A':28+.8*math.sin(i) if i<0 else 48+math.sin(i) if i<4 else 0,'voltage_V':380+.5*math.sin(i)} for i in range(-10,11)]
    event_trace=[{'relative_ms':0,'event':'过流开始','action':'本地延时资格','feedback':'软件输入，未执行现场动作'},
                 {'relative_ms':300,'event':'保护条件成立','action':'模拟锁存 / would_trip','feedback':'等待接触器反馈'},
                 {'relative_ms':400,'event':'模拟辅助触点断开','action':'保持锁存','feedback':'软件反馈到达，非硬件回执'},
                 {'relative_ms':600,'event':'通信中断','action':'本地保护独立保持','feedback':'上行不可用，不解除联锁'}]
    rules=[{'id':'overcurrent','name':'过流','metric':'current_A','pickup':40,'dropout':36,'unit':'A','delay_ms':300,'latch':True,'reset':'本地人工复位'},
           {'id':'imbalance','name':'电流不平衡','metric':'imbalance_pct','pickup':15,'dropout':10,'unit':'%','delay_ms':2000,'latch':True,'reset':'本地人工复位'},
           {'id':'temperature','name':'温升','metric':'temperature_C','pickup':90,'dropout':80,'unit':'℃','delay_ms':5000,'latch':True,'reset':'冷却后本地人工复位'}]
    return {'source_kind':'software_fixture','control_enabled':False,'faults':faults,'event_waveform':event_waveform,'event_trace':event_trace,'rules':rules,'settings_version':'preview-protection-v1','settings_basis':'仅为软件测试阈值，非厂家/现场整定值','maximum_frame_gap_ms':500,'reset_safe_ms':5000,
      'trend':[{'time':f'2026-09-12T02:{i:02d}:00Z','ia':28+1.2*math.sin(i/2),'ib':28.5+1.1*math.sin(i/2+.1),'ic':27.8+1.3*math.sin(i/2-.1)} for i in range(30)],
      'waveform':[{'x':i/512,'y':40*math.sin(2*math.pi*50*i/512)*(1+.03*math.cos(2*math.pi*2*i/512))} for i in range(128)],
      'spectrum':deepcopy(next(r for r in records() if r['kind']=='motor_spectrum')),'protection':deepcopy(next(r for r in records() if r['kind']=='protection_replay')),
      'scenarios':{'normal':{'label':'有转速的软件参考','speed_rpm':1470,'resolution_hz':.25,'finding':'疑似侧频证据，未确认断条'},'missing-speed':{'label':'无同步转速','speed_rpm':None,'resolution_hz':.25,'finding':'不能定位滑差侧频，证据不足'},'low-resolution':{'label':'低频率分辨率','speed_rpm':1470,'resolution_hz':5,'finding':'分辨率不足以区分 2 Hz 侧频，拒绝判定'}}}
