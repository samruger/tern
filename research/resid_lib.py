"""Does spot-based z add info beyond Kalshi mid? logistic y ~ a*logit(mid) + b*z (+c), per horizon, train/test split."""
import numpy as np

def fit(X, y, it=50):
    w = np.zeros(X.shape[1])
    for _ in range(it):  # Newton
        p = 1/(1+np.exp(-X@w)); g = X.T@(y-p); H = (X*(p*(1-p))[:,None]).T@X + 1e-6*np.eye(len(w))
        w += np.linalg.solve(H, g)
    return w
def ll(X, w, y):
    p = np.clip(1/(1+np.exp(-X@w)),1e-5,1-1e-5); return -np.mean(y*np.log(p)+(1-y)*np.log(1-p))

