"""One normal app launch/reopen, then a bounded wait for its conversation."""
import subprocess,time
from .capture import ax
from .relay import RelayFailure


def ensure_startup(ticket,backend,emit=lambda event:None,*,launch=None,clock=time.monotonic,pause=time.sleep):
    ticket.check();deadline=min(ticket.deadline,clock()+30)
    def check():
        ticket.check()
        if clock()>=deadline:raise RelayFailure('startup_window_unavailable')
    def normal_open():
        from .driver.siriai import _SIRI_AI_APP
        result=subprocess.run(['open','-a',_SIRI_AI_APP],capture_output=True,timeout=min(5,max(.001,deadline-clock())))
        if result.returncode:raise RelayFailure('startup_launch_failed')
    pid=backend._find_pid();ticket.check();opened=False
    while True:
        check();current=backend._find_pid();check()
        if pid is not None and current is not None and current!=pid:raise RelayFailure('startup_process_replaced')
        if current is not None:
            pid=current;windows=backend._windows(ax.app_for_pid(pid));ready=[]
            for window in windows:
                root=ax.snapshot_tree(window,structural=True)
                if not root.complete:raise RelayFailure('startup_incomplete_structure')
                composer=ax.find_composer_node(root)
                if composer is not None and ax.conversation_container(root,composer) is not None:ready.append(window)
            if len(ready)>1:raise RelayFailure('startup_ambiguous_conversations')
            if len(ready)==1:
                if len(windows)!=1:raise RelayFailure('startup_unowned_extra_window')
                emit({'phase':'startup_conversation_available','normal_open_requested':opened})
                return pid
        if not opened:
            check();opened=True;(launch or normal_open)()
            emit({'phase':'startup_normal_open_requested','action_repeated':False})
        pause(.1)
