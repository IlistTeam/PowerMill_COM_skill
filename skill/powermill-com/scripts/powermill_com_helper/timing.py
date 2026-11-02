"""Opt-in, in-memory timing. No I/O, COM calls, retries or safety decisions."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import copy,time

_active=ContextVar('powermill_timing',default=None)

def enabled():return _active.get() is not None

class Profiler:
    def __init__(self):
        self.spans=[];self.worker=[];self._stack=ContextVar('powermill_timing_stack',default=())
    def __enter__(self):
        self._token=_active.set(self);return self
    def __exit__(self,*exc):_active.reset(self._token)
    def snapshot(self):
        rows=copy.deepcopy(self.spans);children={}
        for row in rows:
            if row['parent'] is not None:children[row['parent']]=children.get(row['parent'],0)+row['seconds']
        totals={}
        for row in rows:
            row['self_seconds']=max(0.,row['seconds']-children.get(row['id'],0))
            item=totals.setdefault(row['name'],{'count':0,'seconds':0.,'self_seconds':0.})
            item['count']+=1;item['seconds']+=row['seconds'];item['self_seconds']+=row['self_seconds']
        return {'schema':1,'scope':'Inclusive spans nest; sum self_seconds, not overlapping inclusive totals. Worker counters nest separately inside transport roundtrips. No profiling file is written until the caller explicitly exports this snapshot.',
                'totals':totals,'spans':rows,'worker_requests':copy.deepcopy(self.worker)}

@contextmanager
def span(name,**details):
    profiler=_active.get()
    if profiler is None:
        yield;return
    stack=profiler._stack.get();row={'id':len(profiler.spans),'parent':stack[-1] if stack else None,'name':name,'details':details}
    profiler.spans.append(row);token=profiler._stack.set(stack+(row['id'],));start=time.perf_counter()
    try:yield
    except BaseException:
        row['failed']=True;raise
    finally:
        row['seconds']=time.perf_counter()-start;profiler._stack.reset(token)

def timed(name):
    def decorate(function):
        @wraps(function)
        def measured(*args,**kwargs):
            with span(name):return function(*args,**kwargs)
        return measured
    return decorate

def worker_sample(method,data):
    profiler=_active.get()
    if profiler is not None and isinstance(data,dict):
        stack=profiler._stack.get();profiler.worker.append({'method':method,'parent_span':stack[-1] if stack else None,'timing':copy.deepcopy(data)})
