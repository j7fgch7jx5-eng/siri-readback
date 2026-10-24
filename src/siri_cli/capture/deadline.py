"""Bound AX reads in a process that owns the Siri transaction lock."""
from contextlib import contextmanager
import time


@contextmanager
def bounded_ax_reads(deadline, *, api=None, clock=time.monotonic):
    if api is None:
        import ApplicationServices as api
    originals={}
    try:
        for name in ('AXUIElementCopyAttributeValue','AXUIElementCopyAttributeNames',
                     'AXUIElementCopyActionNames'):
            original=getattr(api,name)
            originals[name]=original
            def read(ref,*args,_call=original):
                remaining=deadline-clock()
                if remaining<=0:raise TimeoutError('preview deadline reached')
                error=api.AXUIElementSetMessagingTimeout(ref,min(5,remaining))
                if error:raise ValueError(f'AX messaging timeout could not be bounded ({error})')
                return _call(ref,*args)
            setattr(api,name,read)
        yield
    finally:
        for name,original in originals.items():setattr(api,name,original)
