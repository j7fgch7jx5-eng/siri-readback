"""Content-free observed controls/state, never a completion detector."""


def describe_surface(api,ref):
    error,names=api.AXUIElementCopyAttributeNames(ref,None)
    if error:return {'attribute_names_error':int(error)}
    names=set(str(n) for n in names or [])
    states={}
    for name in ('AXBusy','AXElementBusy','AXEnabled','AXLiveRegionStatus'):
        if name not in names:states[name]={'supported':False};continue
        code,value=api.AXUIElementCopyAttributeValue(ref,name,None)
        state={'supported':True,'error':int(code)}
        if not code:
            if isinstance(value,(bool,int,float)):state['value']=value
            elif str(value) in ('off','polite','assertive'):state['value']=str(value)
            else:state['value_type']=type(value).__name__
        states[name]=state
    error,actions=api.AXUIElementCopyActionNames(ref,None)
    allowed={'AXPress','AXConfirm','AXCancel','AXShowMenu','AXScrollToVisible'}
    return {'states':states,'actions_error':int(error),
            'actions':[str(a) for a in actions or [] if str(a) in allowed],
            'other_action_count':sum(str(a) not in allowed for a in actions or []),
            'completion':'unverified'}
