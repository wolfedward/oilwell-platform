"""Hand equations for tests; no imports of production numerical kernels."""
from fractions import Fraction as Q
import math


def numerator(power,theta,omega,alpha,*,loss=1200,upstream=30,crank=200,moment=1800,friction=300):
    return (power-loss)/omega+9.80665*moment*math.cos(theta)-(upstream+crank)*alpha-friction-100*omega


def jacobian(theta):
    # Implicit constraint derivative using a separately solved Cartesian joint.
    px,py=.5*math.sin(theta),.5*math.cos(theta)
    dx,dy=px-3,py-2; rho=math.hypot(dx,dy)
    a=(4-9+rho*rho)/(2*rho); h=math.sqrt(4-a*a)
    ux,uy=(a*dx-h*dy)/rho,(a*dy+h*dx)/rho
    ex,ey=3+ux-px,2+uy-py
    return 6*(ex*.5*math.cos(theta)-ey*.5*math.sin(theta))/(ex*(-uy)+ey*ux)


def rational_numerator_at_zero(power=Q(10000),omega=Q(4,5),alpha=Q(1,10)):
    return (power-1200)/omega+Q('9.80665')*1800-230*alpha-300-100*omega
