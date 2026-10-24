"""Failed AX metadata must not pass as absent current-turn content."""
import sys
from types import SimpleNamespace
from unittest.mock import patch
from siri_cli.capture import ax
from test_turn_scope import fails


def api(failed_attribute=None, action_error=0, role='AXStaticText'):
    def read(raw, name, unused):
        if name == failed_attribute:
            return -25202, None
        if name == 'AXRole':
            return 0, role
        return -25205, None
    return SimpleNamespace(
        kAXErrorAttributeUnsupported=-25205, kAXErrorNoValue=-25212,
        AXUIElementCopyAttributeNames=lambda *args:(0, ['AXRole','AXIdentifier','AXValue',
            'AXDescription','AXTitle','AXPosition','AXSize','AXURL','AXFilename','AXDocument']),
        AXUIElementCopyAttributeValue=read,
        AXUIElementCopyActionNames=lambda *args:(action_error, []))


def test_failed_identity_or_content_read_rejects_snapshot():
    for name in ('AXRole', 'AXIdentifier', 'AXValue', 'AXPosition'):
        with patch.dict(sys.modules, ApplicationServices=api(name)):
            fails(lambda:ax.snapshot_tree(object()), ValueError)


def test_failed_actions_read_rejects_snapshot():
    with patch.dict(sys.modules, ApplicationServices=api(action_error=-25202,role='AXTextField')):
        fails(lambda:ax.snapshot_tree(object()), ValueError)


def test_optional_actions_failure_is_recorded_instead_of_hidden():
    with patch.dict(sys.modules, ApplicationServices=api(action_error=-25200,role='AXGroup')):
        node=ax.snapshot_tree(object())
    assert node.element.actions_error == -25200
    assert node.element.actions == ()


def test_optional_payload_failure_is_explicit_diagnostic_data():
    with patch.dict(sys.modules, ApplicationServices=api(failed_attribute='AXURL')):
        node=ax.snapshot_tree(object())
    assert node.element.url is None
    assert node.element.attribute_errors['AXURL'] == -25202


def test_explicitly_unsupported_metadata_remains_valid():
    with patch.dict(sys.modules, ApplicationServices=api()):
        node=ax.snapshot_tree(object())
    assert node.complete and node.element.role == 'AXStaticText'
    assert node.element.url is None and not node.children


def test_required_role_cannot_be_absent():
    mock=api()
    mock.AXUIElementCopyAttributeValue=lambda *args:(-25205, None)
    with patch.dict(sys.modules, ApplicationServices=mock):
        fails(lambda:ax.snapshot_tree(object()), ValueError)


def test_unadvertised_optional_url_is_not_queried():
    mock=api(failed_attribute='AXURL')
    mock.AXUIElementCopyAttributeNames=lambda *args:(0, ['AXRole'])
    original=mock.AXUIElementCopyAttributeValue
    calls=[]
    def read(raw,name,unused):
        calls.append(name)
        return original(raw,name,unused)
    mock.AXUIElementCopyAttributeValue=read
    with patch.dict(sys.modules,ApplicationServices=mock):
        node=ax.snapshot_tree(object())
    assert 'AXURL' not in calls and node.element.url is None


def test_failed_attribute_names_is_not_treated_as_no_attributes():
    mock=api()
    mock.AXUIElementCopyAttributeNames=lambda *args:(-25202,None)
    with patch.dict(sys.modules,ApplicationServices=mock):
        fails(lambda:ax.snapshot_tree(object()), ValueError)


def test_structural_pass_never_requests_history_content_or_payloads():
    mock=api()
    original=mock.AXUIElementCopyAttributeValue
    calls=[]
    def read(raw,name,unused):
        calls.append(name)
        return original(raw,name,unused)
    mock.AXUIElementCopyAttributeValue=read
    with patch.dict(sys.modules,ApplicationServices=mock):
        node=ax.snapshot_tree(object(),structural=True)
    assert not set(calls) & {'AXValue','AXTitle','AXDescription','AXURL','AXDocument','AXFilename'}
    assert node.element.value is None


def test_nonfinite_optional_geometry_retains_node_and_explicit_error():
    for value in (float('inf'),float('-inf'),float('nan')):
        mock=api();original=mock.AXUIElementCopyAttributeValue
        mock.kAXValueCGPointType=1;mock.kAXValueCGSizeType=2
        mock.AXValueGetValue=lambda raw,kind,unused:(True,SimpleNamespace(x=value,y=0) if kind==1 else SimpleNamespace(width=100,height=100))
        mock.AXUIElementCopyAttributeValue=lambda raw,name,unused:((0,name) if name in ('AXPosition','AXSize') else original(raw,name,unused))
        with patch.dict(sys.modules,ApplicationServices=mock):node=ax.snapshot_tree(object())
        assert node.complete and node.element.role=='AXStaticText'
        assert node.element.bounds is None
        assert node.element.attribute_errors['AXBounds']=='non_finite'


def test_nonfinite_selected_photo_geometry_remains_rejected():
    from siri_cli.photo_preview import select_single_photo
    from test_photo_provider import recorded
    response=recorded();response.children[1].children[0].element.bounds=None
    fails(lambda:select_single_photo(response,(0,0,2000,2000)),ValueError)
