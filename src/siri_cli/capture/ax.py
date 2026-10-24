"""AX tree extraction via pyobjc's ApplicationServices.

Walks the macOS Accessibility (AX) UI tree for a process and extracts
text/values within a region, so a Siri/Spotlight response can be read out
of the rendered UI.

Lazy-imports ApplicationServices so the package still imports cleanly in
pure-logic unit tests on any platform.

"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Iterator, List, Optional

# Roles that carry user-visible text we care about.
_TEXT_ROLES = {
    "AXStaticText",
    "AXTextField",
    "AXTextArea",
    "AXButton",
    "AXLink",
    "AXHeading",
    "AXCell",
    "AXValueIndicator",
    "AXMenuItem",
    "AXLabel",
}


class AXReadError(ValueError):
    """Strict read failure with diagnostics independent of readable attributes."""
    def __init__(self, operation, error_code):
        self.operation = operation
        self.error_code = int(error_code)
        self.structural_path = None
        self.snapshot_context = None
        super().__init__(f'{operation} read failed ({error_code}); incomplete snapshot')


def ax_read_failure(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,AXReadError):
            result = {'operation':error.operation,'error_code':error.error_code,
                    'structural_path':list(error.structural_path) if error.structural_path is not None else None,
                    'path_origin':'snapshot_root','node_attributes_required_for_path':False}
            if error.snapshot_context is not None:
                result['snapshot_context'] = error.snapshot_context
            return result
        error=error.__cause__
    return None


@dataclasses.dataclass
class AXElement:
    """Minimal snapshot of an AX element."""

    role: str
    value: Optional[str]
    description: Optional[str]
    title: Optional[str]
    # Native point coords: [x, y, w, h]
    bounds: Optional[tuple[int, int, int, int]] = None
    # Native payload attributes; identifiers are metadata, not file paths.
    url: Optional[str] = None
    filename: Optional[str] = None
    document: Optional[str] = None
    identifier: Optional[str] = None
    actions: tuple = ()
    actions_error: Optional[int] = None
    attribute_errors: dict = dataclasses.field(default_factory=dict)

    @property
    def label(self) -> Optional[str]:
        """Best human-readable text for the element."""
        return self.title or self.value or self.description

    @property
    def text(self) -> Optional[str]:
        """Element text if this role carries visible text."""
        if self.role in _TEXT_ROLES:
            return self.label
        return None


@dataclasses.dataclass
class AXNode:
    """Snapshot tree node: an AXElement plus its children.

    `ref` holds the raw AXUIElement on a live session (None in tests)."""

    element: AXElement
    children: List["AXNode"] = dataclasses.field(default_factory=list)
    ref: Any = None
    complete: bool = True


def _get_attr(el, attr: str):
    """Safely read an AX attribute; returns None on failure."""
    import ApplicationServices as ax

    err, val = ax.AXUIElementCopyAttributeValue(el, attr, None)
    if err != 0:  # kAXErrorSuccess
        return None
    return val


def _get_attr_checked(el, attr: str):
    """Allow absent attributes, but never hide a failed AX transport read."""
    import ApplicationServices as ax

    err, value = ax.AXUIElementCopyAttributeValue(el, attr, None)
    if err == 0:
        return value
    if err in (ax.kAXErrorAttributeUnsupported, ax.kAXErrorNoValue):
        return None
    raise AXReadError(attr, err)


def _get_attr_text(el, attr: str, *, reader=_get_attr) -> Optional[str]:
    """Read an attribute and coerce to str/None."""
    val = reader(el, attr)
    if val is None:
        return None
    s = str(val)
    return s if s else None


def _element_bounds(el, *, reader=_get_attr, errors=None) -> Optional[tuple[int, int, int, int]]:
    """Decode AXValue-wrapped CGPoint/CGSize in screen point coordinates."""
    import ApplicationServices as ax
    pos, size = reader(el, "AXPosition"), reader(el, "AXSize")
    if pos is None or size is None:
        return None
    try:
        ok_p, point = ax.AXValueGetValue(pos, ax.kAXValueCGPointType, None)
        ok_s, extent = ax.AXValueGetValue(size, ax.kAXValueCGSizeType, None)
        if not (ok_p and ok_s):
            return None
        values=(point.x,point.y,extent.width,extent.height)
        if not all(math.isfinite(v) for v in values):
            if errors is not None:errors['AXBounds']='non_finite'
            return None
        return tuple(int(v) for v in values)
    except OverflowError:
        if errors is not None:errors['AXBounds']='numeric_overflow'
        return None
    except (TypeError, ValueError, AttributeError):
        return None


def _element_to_snapshot(el, *, strict=False, structural=False) -> AXElement:
    reader = _get_attr_checked if strict else _get_attr
    attribute_errors = {}
    if strict:
        import ApplicationServices as _api
        err, names = _api.AXUIElementCopyAttributeNames(el, None)
        if err:
            raise AXReadError('AXUIElementCopyAttributeNames', err)
        supported = set(str(name) for name in names or ())
        def reader(raw, attr):
            if attr not in supported:
                return None
            if attr in ('AXURL', 'AXFilename', 'AXDocument'):
                # Payload discovery is optional. A failed discovery read is
                # diagnostic data, never evidence that no payload exists.
                error, value = _api.AXUIElementCopyAttributeValue(raw, attr, None)
                if error:
                    attribute_errors[attr] = int(error)
                    return None
                return value
            return _get_attr_checked(raw, attr)
    def text(attr):
        return _get_attr_text(el, attr, reader=reader)
    role = text("AXRole") or ""
    if strict and not role:
        raise ValueError("AXRole unavailable; incomplete snapshot")
    # Action names come from AXUIElementCopyActionNames, NOT an attribute —
    # AXUIElementCopyAttributeValue(el, "AXActionNames") always errors.
    # Buttons and panes also expose actions needed for capability inspection.
    actions: tuple = ()
    actions_error = None
    if role:
        import ApplicationServices as _ax

        _err, _names = _ax.AXUIElementCopyActionNames(el, None)
        if _err:
            actions_error = int(_err)
        # Composer identity depends on its action list. Other roles may not
        # implement action enumeration; preserve that error as diagnostic data.
        if strict and _err and role == 'AXTextField':
            raise AXReadError('AXUIElementCopyActionNames', _err)
        if _err == 0 and _names is not None:
            actions = tuple(str(n) for n in _names)
    if structural:
        return AXElement(role=role, value=None, description=None, title=None,
                         identifier=text('AXIdentifier'), actions=actions,
                         actions_error=actions_error)
    return AXElement(
        role=role,
        value=text("AXValue"),
        description=text("AXDescription"),
        title=text("AXTitle"),
        bounds=_element_bounds(el, reader=reader, errors=attribute_errors),
        url=text("AXURL"),
        filename=text("AXFilename"),
        document=text("AXDocument"),
        identifier=text("AXIdentifier"),
        actions=actions,
        actions_error=actions_error,
        attribute_errors=attribute_errors,
    )


def _children(el) -> Iterator:
    """Iterate direct AX children of an element."""
    import ApplicationServices as ax

    err, arr = ax.AXUIElementCopyAttributeValue(el, "AXChildren", None)
    if err != 0 or arr is None:
        return
    try:
        for child in arr:
            yield child
    except TypeError:  # pragma: no cover - non-iterable
        return


def walk_tree(el) -> Iterator[AXElement]:
    """Depth-first walk yielding AXElement snapshots (parents before children)."""
    yield _element_to_snapshot(el)
    for child in _children(el):
        yield from walk_tree(child)


def snapshot_tree(el, *, max_depth: int = 40, structural: bool = False) -> AXNode:
    """Build an AXNode snapshot tree from a raw AX element (live sessions)."""

    # Retain only roles and known layout identifiers, never labels/values or
    # arbitrary identifiers (which can contain filenames or personal data).
    layout_ids = {'innerChatSessionView', 'chatSessionView', 'promptView',
                  'promptViewTextField', 'userPrompt', 'modelResponse',
                  'modelResponse-richTextParagraph', 'modelResponse-richTextCustomView',
                  'chatGridView'}

    def _build(raw, depth: int, path: tuple, ancestors: tuple) -> AXNode:
        node = None
        try:
            node = AXNode(element=_element_to_snapshot(raw, strict=True, structural=structural), ref=raw)
            import ApplicationServices as ax
            err, children = ax.AXUIElementCopyAttributeValue(raw, "AXChildren", None)
            if err not in (0, ax.kAXErrorAttributeUnsupported, ax.kAXErrorNoValue):
                raise AXReadError('AXChildren', err)
            children = list(children) if children is not None else []
        except AXReadError as error:
            # The child ordinal is known before any attributes of it are read.
            # Preserve it without retrying, querying the failed node or skipping it.
            error.structural_path = path
            error.snapshot_context = {
                'mode': 'structural' if structural else 'content',
                'ancestors': list(ancestors),
                'failing_node_snapshot_available': node is not None,
                'partial_snapshot_returned': False,
            }
            raise
        if depth < max_depth:
            descriptor = {'path': list(path), 'role': node.element.role,
                          'identifier': node.element.identifier if node.element.identifier in layout_ids else None}
            node.children = [_build(c, depth + 1, path + (i,), ancestors + (descriptor,)) for i, c in enumerate(children)]
        else:
            node.complete = not children
        node.complete = node.complete and all(c.complete for c in node.children)
        return node

    return _build(el, 0, (), ())


def find_element(
    app_ref, *,
    role: Optional[str] = None,
    label_contains: Optional[str] = None,
    max_depth: int = 40,
) -> Optional[AXElement]:
    """Find the first element matching role/label in an app's AX tree.

    `app_ref` is the result of `ApplicationServices.AXUIElementCreateApplication(pid)`.
    """
    found = None

    def _walk(el, depth):
        nonlocal found
        if found is not None or depth > max_depth:
            return
        snap = _element_to_snapshot(el)
        if role and snap.role != role:
            pass  # still recurse
        elif label_contains and snap.label and label_contains.lower() in snap.label.lower():
            found = snap
            return
        elif role and not label_contains and snap.role == role:
            found = snap
            return
        for child in _children(el):
            _walk(child, depth + 1)
            if found is not None:
                return

    _walk(app_ref, 0)
    return found


def extract_texts(app_ref, *, max_depth: int = 40, limit: int = 500) -> List[str]:
    """Collect user-visible text from an app's AX tree, in tree order."""
    texts: List[str] = []
    count = 0

    def _walk(el, depth):
        nonlocal count
        if depth > max_depth or count >= limit:
            return
        snap = _element_to_snapshot(el)
        t = snap.text
        if t:
            texts.append(t)
            count += 1
        for child in _children(el):
            _walk(child, depth + 1)

    _walk(app_ref, 0)
    return texts


# ---------------------------------------------------------------------------
# PATCH: pure, testable helpers over AXNode snapshot trees (turn scoping).
# ---------------------------------------------------------------------------


def is_composer_element(el: AXElement) -> bool:
    """True for the composer text field (same predicate as the driver uses)."""
    return el.role == "AXTextField" and ("AXConfirm" in el.actions or "AXFocus" in el.actions)


def nodes(root: AXNode) -> Iterator[AXNode]:
    yield root
    for child in root.children:
        yield from nodes(child)


def find_composer_node(root: AXNode) -> Optional[AXNode]:
    """Require a unique composer; multiple windows must never choose by order."""
    found = [n for n in nodes(root) if is_composer_element(n.element)]
    return found[0] if len(found) == 1 else None


def conversation_container(root: AXNode, composer: AXNode) -> Optional[AXNode]:
    """Use the live Siri pane identifier, never a generic window ancestor.

    Observed on macOS 27: chatGridView and innerChatSessionView are sibling
    branches beneath AXSplitGroup. Unknown layouts fail closed.
    """
    path = _path_to(root, composer)
    if not path or not root.complete:
        return None
    panes = [n for n in path if n.element.identifier == "innerChatSessionView"]
    if len(panes) != 1 or not any(n.element.role == "AXWindow" for n in path):
        return None
    pane = panes[0]
    if any(n.element.identifier in ("chatGridView", "gridItemView") for n in nodes(pane)):
        return None
    transcripts = [n for n in nodes(pane) if n.element.identifier == "chatSessionView"]
    if len(transcripts) != 1:
        return None
    return pane


def transcript_container(pane: AXNode) -> AXNode:
    found = [n for n in nodes(pane) if n.element.identifier == "chatSessionView"]
    if len(found) != 1:
        raise ValueError("unique chatSessionView required")
    return found[0]


def _path_to(root: AXNode, target: AXNode) -> Optional[List[AXNode]]:
    if root is target:
        return [root]
    for child in root.children:
        sub = _path_to(child, target)
        if sub is not None:
            return [root] + sub
    return None


def extract_scoped_texts(container: AXNode, *, skip: Optional[AXNode] = None, limit: int = 500) -> List[str]:
    """Texts from the container subtree only, skipping the `skip` subtree.

    Scoping is structural: nothing outside the container (sidebar history,
    other windows) can leak into the result, regardless of timing."""
    texts: List[str] = []

    def _walk(node: AXNode) -> None:
        if node is skip:
            return
        if len(texts) >= limit:
            raise ValueError("scoped text limit reached; refusing partial readback")
        t = node.element.text
        if t:
            texts.append(t)
        for child in node.children:
            _walk(child)

    _walk(container)
    return texts


def probe_result_cards(root: AXNode) -> List[AXElement]:
    """Elements carrying file/link payloads (AXURL/AXFilename/AXDocument).

    Capability probe only. An identifier alone is never a usable payload.
    Absence means these attributes were not exposed in this snapshot."""
    out: List[AXElement] = []

    def _walk(node: AXNode) -> None:
        el = node.element
        if el.url or el.filename or el.document:
            out.append(el)
        for child in node.children:
            _walk(child)

    _walk(root)
    return out


def app_for_pid(pid: int):
    """Create an AXUIElement for the given pid."""
    import ApplicationServices as ax

    return ax.AXUIElementCreateApplication(pid)


def is_trusted() -> bool:
    """True if this process is AX-trusted."""
    import ApplicationServices as ax

    return bool(ax.AXIsProcessTrusted())
