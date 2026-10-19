"""Instantaneous three-phase synthetic signals; no physical fault confirmation."""
import math
from .contracts import Channel,PackConfig
from .cycles import quantize
from .waveforms import encode_current


def generate_currents(config:PackConfig):
    names=['sine','harmonic','sidebands','gap']
    rate=1024
    if config.profile=='extended':
        names+=['amplitude','unbalance','clipped','frequency-offset']
        rate=2048
    cases=[]
    for name in names:
        channels=[]
        for phase in range(3):
            raw,quality=[],[]
            amplitude=12 if name=='amplitude' else (8 if name=='unbalance' and phase==1 else 10)
            frequency=50.25 if name=='frequency-offset' else 50
            phi=-phase*math.tau/3
            for i in range(16*rate):
                t=i/rate
                value=amplitude*math.sin(math.tau*frequency*t+phi)
                if name=='harmonic':
                    value+=.5*math.sin(math.tau*150*t+3*phi)
                elif name=='sidebands':
                    value+=.5*(math.sin(math.tau*48*t+phi)+math.sin(math.tau*52*t+phi))
                q=0
                if name=='gap' and rate*4<=i<rate*4+32:
                    value,q=0,1
                elif name=='clipped' and abs(value)>8:
                    value,q=math.copysign(8,value),2
                raw.append(quantize(value,'0.0001'));quality.append(q)
            channels.append(Channel('current_'+chr(97+phase),'A','0.0001','0',tuple(raw),tuple(quality)))
        cases.append(encode_current('current-'+name,config,tuple(channels),rate,
                     {'signal_case':name,'phase_offsets_rad':[0,-math.tau/3,-2*math.tau/3],
                      'base_frequency_hz':frequency,'fundamental_peak_A':10,'quantization_A':.0001,
                      'missing_indices':[4*rate,4*rate+31] if name=='gap' else [],
                      'fault_confirmation':False,'anti_aliasing':'analytic band-limited signal; no ADC/filter measured'}))
    return tuple(cases)
