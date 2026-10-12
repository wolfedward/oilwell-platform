from datetime import datetime,timedelta
import hashlib
import math


def supplementary(well,day):
    if not well or not day:return {'fluid_level':[],'haulage':[],'status_history':[]}
    seed=int(hashlib.sha256(well['id'].encode()).hexdigest()[:8],16)
    start=datetime.fromisoformat(day)
    level=[];haulage=[]
    for i in range(24):
        stamp=(start+timedelta(hours=i)).isoformat()+'+08:00'
        level.append({'id':f'preview-level-{well["id"]}-{i}','well_id':well['id'],'time':stamp,'value':round(790+seed%110+25*math.sin(i*.35)+3*math.sin(i*1.2),2),
                      'unit':'m','quality':'good','source_kind':'software_fixture','reference':'井口以下深度','alarm':'warning' if i in (14,15) else 'normal'})
        if i%4==0:haulage.append({'id':f'preview-tank-{well["id"]}-{i}','tank_id':'PREVIEW-TANK-'+well['id'],'well_id':well['id'],'time':stamp,
                                'level_m':round(1.3+.5*math.sin(i*.4),2),'volume_m3':round(8+3*math.sin(i*.4),2),'capacity_m3':15,
                                'operation':'装卸记录' if i in (8,16) else '液位记录','source_kind':'software_fixture'})
    states=[{'time':day+'T00:00:00+08:00','status':'running','reason':'测试班次运行','duration':'8 h','actor':'预览操作者'},
            {'time':day+'T08:00:00+08:00','status':'maintenance','reason':'测试巡检','duration':'30 min','actor':'预览运维'},
            {'time':day+'T08:30:00+08:00','status':well['status'],'reason':'测试场景恢复','duration':'至数据时点','actor':'预览运维'}]
    return {'fluid_level':level,'haulage':haulage,'status_history':states}
