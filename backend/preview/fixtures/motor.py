from copy import deepcopy
import math
from .studies import records
def fixture():
    faults=[{'id':'preview-M01','name':'三相运行正常','ia':28.3,'ib':28.7,'ic':28.1,'voltage':381.2,'temperature':62.4,'severity':'正常','evidence':'三相 RMS 接近，测试温度稳定'},
    {'id':'preview-M02','name':'缺相测试','ia':1.2,'ib':38.4,'ic':39.1,'voltage':380.7,'temperature':68.1,'severity':'保护锁存','evidence':'A 相电流缺失；需核对 CT 接线与一次回路'},
    {'id':'preview-M03','name':'电流不平衡','ia':21.8,'ib':32.4,'ic':35.1,'voltage':379.8,'temperature':71.2,'severity':'告警','evidence':'相电流最大偏差增大，不能单独判定绕组损坏'},
    {'id':'preview-M04','name':'温升异常','ia':31.2,'ib':31.8,'ic':31.1,'voltage':380.2,'temperature':96.5,'severity':'延时资格','evidence':'测试温度超过整定值，待持续时间确认'}]
    rules=[{'id':'overcurrent','name':'过流','metric':'current_A','pickup':40,'dropout':36,'unit':'A','delay_ms':300,'latch':True,'reset':'本地人工复位'},
           {'id':'imbalance','name':'电流不平衡','metric':'imbalance_pct','pickup':15,'dropout':10,'unit':'%','delay_ms':2000,'latch':True,'reset':'本地人工复位'},
           {'id':'temperature','name':'温升','metric':'temperature_C','pickup':90,'dropout':80,'unit':'℃','delay_ms':5000,'latch':True,'reset':'冷却后本地人工复位'}]
    return {'source_kind':'software_fixture','control_enabled':False,'faults':faults,'rules':rules,'settings_version':'preview-protection-v1','settings_basis':'仅为软件测试阈值，非厂家/现场整定值','maximum_frame_gap_ms':500,'reset_safe_ms':5000,
      'trend':[{'time':f'2026-09-12T02:{i:02d}:00Z','ia':28+1.2*math.sin(i/2),'ib':28.5+1.1*math.sin(i/2+.1),'ic':27.8+1.3*math.sin(i/2-.1)} for i in range(30)],
      'waveform':[{'x':i/512,'y':40*math.sin(2*math.pi*50*i/512)*(1+.03*math.cos(2*math.pi*2*i/512))} for i in range(128)],
      'spectrum':deepcopy(next(r for r in records() if r['kind']=='motor_spectrum')),'protection':deepcopy(next(r for r in records() if r['kind']=='protection_replay')),
      'scenarios':{'normal':{'label':'有转速的软件参考','speed_rpm':1470,'resolution_hz':.25,'finding':'疑似侧频证据，未确认断条'},'missing-speed':{'label':'无同步转速','speed_rpm':None,'resolution_hz':.25,'finding':'不能定位滑差侧频，证据不足'},'low-resolution':{'label':'低频率分辨率','speed_rpm':1470,'resolution_hz':5,'finding':'分辨率不足以区分 2 Hz 侧频，拒绝判定'}}}
