"""Prepare native fullscreen before submission; never infer response completion."""


def current_fullscreen_window(api, windows, remaining, pump):
    """Wait through the native transition, then bind the current window once."""
    while True:
        remaining()
        current = windows()
        if len(current) != 1:
            raise RuntimeError('ambiguous Siri windows during fullscreen preparation')
        error, value = api.AXUIElementCopyAttributeValue(current[0], 'AXFullScreen', None)
        if error:
            raise RuntimeError(f'current fullscreen verification failed ({error})')
        if value:
            return current[0]
        pump(min(.1, remaining()))


def ensure_fullscreen(api, window, remaining, pump):
    remaining()
    error, value = api.AXUIElementCopyAttributeValue(window, 'AXFullScreen', None)
    if error:
        raise RuntimeError(f'fullscreen read failed ({error})')
    changed = not bool(value)
    if changed:
        error, writable = api.AXUIElementIsAttributeSettable(window, 'AXFullScreen', None)
        if error or not writable:
            raise RuntimeError('fullscreen attribute is not writable')
        if api.AXUIElementSetAttributeValue(window, 'AXFullScreen', True):
            raise RuntimeError('fullscreen transition failed; no retry')
    while True:
        remaining()
        error, value = api.AXUIElementCopyAttributeValue(window, 'AXFullScreen', None)
        if error:
            raise RuntimeError(f'fullscreen verification failed ({error})')
        if value:
            return {'fullscreen': True, 'changed': changed}
        pump(min(.1, remaining()))
