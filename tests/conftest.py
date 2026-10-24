"""Isolate upstream AX tests that replace module helpers without undoing them."""
import pytest
from siri_cli.capture import ax


_snapshot = ax._element_to_snapshot
_children = ax._children


@pytest.fixture(autouse=True)
def isolate_ax_helpers():
    ax._element_to_snapshot = _snapshot
    ax._children = _children
    try:
        yield
    finally:
        ax._element_to_snapshot = _snapshot
        ax._children = _children
