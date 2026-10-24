from slowapi import Limiter
from slowapi.util import get_remote_address

# in-memory counters: per process, reset on restart (fine for one API server)
limiter = Limiter(key_func=get_remote_address)
