"""Token -> driver (S2/S4). The model never sees the token or driver_id."""
from database import driver_by_token

def require_driver(c, token):
    d = driver_by_token(c, token or "")
    if not d: raise PermissionError("invalid token")
    return d
