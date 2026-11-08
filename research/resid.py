"""Does spot-based z add info beyond Kalshi mid? logistic y ~ a*logit(mid) + b*z (+c), per horizon, train/test split."""
import numpy as np
from model import snapshots

def fit(X, y, it=50):
    w = np.zeros(X.shape[1])
    for _ in range(it):  # Newton
        p = 1/(1+np.exp(-X@w)); g = X.T@(y-p); H = (X*(p*(1-p))[:,None]).T@X + 1e-6*np.eye(len(w))
        w += np.linalg.solve(H, g)
    return w
def ll(X, w, y):
    p = np.clip(1/(1+np.exp(-X@w)),1e-5,1-1e-5); return -np.mean(y*np.log(p)+(1-y)*np.log(1-p))

for lag in [0, 2, 5]:
  for series, sym in [("KXBTC15M","BTCUSDT"),("KXETH15M","ETHUSDT")]:
    a = snapshots(series, sym, lag)
    tau,bid,ask,z,y,o = a.T
    mid=(bid+ask)/2; lm=np.log(mid/(1-mid)); z=np.clip(z,-8,8)
    split=np.quantile(np.unique(o),0.5); tr=o<split; te=~tr
    line=[]
    for lo,hi in [(600,901),(300,600),(120,300),(1,120)]:
        k=(tau>=lo)&(tau<hi)
        X0=np.c_[lm,np.ones_like(lm)]; X1=np.c_[lm,z,np.ones_like(lm)]
        w0=fit(X0[k&tr],y[k&tr]); w1=fit(X1[k&tr],y[k&tr])
        d=ll(X0[k&te],w0,y[k&te])-ll(X1[k&te],w1,y[k&te])
        line.append(f"[{lo},{hi}) b_z={w1[1]:+.2f} dLL={d*1000:+.1f}m")
    print(f"lag={lag} {series}: "+"  ".join(line), flush=True)
