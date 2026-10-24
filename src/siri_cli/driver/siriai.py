"""Siri AI backend (macOS 27).

Drives the new "Siri AI.app" — a real, AX-accessible chat application (unlike
the macOS 26 NC overlay whose AX tree was empty).

Background-first flow (default: does NOT bring Siri to the front, so you can
keep working in your terminal/editor):

1. Ensure the Siri AI app is running (launch if needed; never front it).
2. New conversation via the AX "New Conversation" button (AXPress — no
   keyboard focus required, so it works while another app is frontmost).
3. Type the query via the AX composer (set AXValue + AXConfirm — focus-free).
4. Observe the current response without emitting progress as an answer.
5. Fail closed unless completion can be verified (currently unavailable).

Everything runs through Accessibility, so it works with the app minimized,
behind other windows, or in a corner. Pass `foreground=True` only when you
want Siri to steal the front (not the default).

Readback is restricted to the observed innerChatSessionView/chatSessionView
structure. Unknown, ambiguous, or stale layouts fail closed. The live app
has not exposed a verified completion signal; elapsed stability must never
be promoted to a successful SiriResponse.

"""

from __future__ import annotations

import subprocess
import time
from typing import Optional

from . import base
from ..capture import ax

_SIRI_AI_APP = "/System/Applications/Siri AI.app"
_SIRI_AI_APPNAME = "Siri AI"
_SIRI_AI_BUNDLE = "com.apple.campo"
_SIRI_AI_PID_CMD = ["pgrep", "-f", "Siri AI.app/Contents/MacOS/Siri AI"]


def _norm(s: str) -> str:
    """Whitespace-collapsed form used only to compare prompt provenance."""
    return " ".join(s.split())


class SiriAiBackend(base.BridgeBackend):
    """Backend that drives the new Siri AI app on macOS 27."""

    name = "siriai"

    def __init__(
        self,
        *,
        settle_kwargs: Optional[dict] = None,
        submit_delay_s: float = 0.3,
        open_delay_s: float = 1.5,
        foreground: bool = False,
    ):
        self._settle_kwargs = settle_kwargs or {}
        self.submit_delay_s = submit_delay_s
        self.open_delay_s = open_delay_s
        self.foreground = foreground

    # -- health -----------------------------------------------------------

    def health(self) -> dict:
        from .. import config

        status = config.check()
        return {
            "name": self.name,
            "accessibility": status.accessibility.granted,
            "siri_ai_present": self._find_pid() is not None,
            "siri_ai_app": _SIRI_AI_APP,
            "ready": status.accessibility.granted and self._find_pid() is not None,
        }

    # -- public API -------------------------------------------------------

    def ask(self, query: str, timeout_s: float = 30.0) -> base.SiriResponse:
        health = self.health()
        if not health["accessibility"]:
            raise base.PermissionsError("accessibility")

        start = time.monotonic()

        # Ensure the app is running; do NOT front it unless foreground=True.
        self._ensure_running()
        if self.foreground:
            self._activate()

        # Make sure the app is in a usable (windowed) state. macOS sometimes
        # relaunches Siri AI windowless, where there is no composer and the AX
        # tree is just the menu bar — queries fail. Reset until a window is up.
        pid = self._ensure_windowed()
        if pid is None:
            raise base.SurfaceError("Siri AI app is not running")

        # Never automatically resubmit a query whose confirmation/completion
        # is uncertain. A retry would create another turn with new provenance.
        return self._ask_once(query, pid, timeout_s, start)

    def _ensure_windowed(self) -> Optional[int]:
        """Return a pid whose app has a real window; reset up to 3 times.

        Siri AI can relaunch in a windowless state (no composer, menu-bar-only
        AX tree). This keeps trying until a window exists or gives up.
        """
        for _ in range(3):
            pid = self._find_pid()
            if pid is None:
                return None
            if self._has_window(pid):
                return pid
            # Windowless — reset and try again.
            self._reset_app()
            time.sleep(1.5)
        pid = self._find_pid()
        return pid if pid is not None and self._has_window(pid) else None

    def _ask_once(self, query: str, pid: int, timeout_s: float, start: float) -> base.SiriResponse:
        """Run the query flow once against the given app instance."""
        # New conversation via the AX "New Conversation" button (focus-free).
        # Fail closed when the button cannot be pressed: without a fresh
        # conversation there is no turn isolation.
        if not self._new_conversation(pid):
            raise base.CaptureError("could not press 'New Conversation'")

        # Check raw transcript content, not the permissive chrome filter.
        # A result named "Today", "Copy", or "3" is still preexisting content.
        pane, composer = self._turn_container(pid)
        transcript = ax.transcript_container(pane)
        if transcript.children or transcript.element.text or composer.element.value:
            raise base.CaptureError("fresh transcript/composer not empty; refusing to submit")

        # Type the query via the AX composer (set value + confirm).
        self._type_query_ax(query)

        # Wait within the bound; currently no validated terminal signal exists.
        deadline = time.monotonic() + timeout_s
        text = self._wait_for_response(pid, deadline, echo=query)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if not text:
            raise base.CaptureError("Timed out waiting for Siri AI response")

        return base.SiriResponse(
            text=text,
            backend=self.name,
            elapsed_ms=elapsed_ms,
            capture_mode="ax",
            raw=text,
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _find_pid() -> Optional[int]:
        try:
            out = subprocess.run(_SIRI_AI_PID_CMD, capture_output=True, text=True, timeout=5)
            lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
            return int(lines[0]) if lines else None
        except Exception:
            return None

    def _ensure_running(self) -> None:
        """Launch the app if it isn't running, with its window available.

        Uses plain `open -a`. (NOT `open -gj` — the `-g` flag suppresses the
        window, leaving Siri windowless with no composer, so queries fail.
        Plain `open -a` opens the window; launch does not itself steal focus
        — verified: the frontmost app stays Terminal after launch.)
        """
        if self._find_pid() is None:
            subprocess.run(
                ["open", "-a", _SIRI_AI_APP], capture_output=True, text=True, timeout=5
            )
            time.sleep(self.open_delay_s)

    def _activate(self) -> None:
        """Activate the Siri AI app via AppKit (brings it to the front).

        Only used when foreground=True. NSRunningApplication with the correct
        bundle ID (com.apple.campo) reliably makes it frontmost.
        """
        from AppKit import (
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
        )

        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(_SIRI_AI_BUNDLE)
        for a2 in apps:
            a2.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            return

    def _new_conversation(self, pid: int) -> bool:
        """Start a new conversation via the AX "New Conversation" button.

        AXPress is focus-independent — it works while the terminal is
        frontmost, so Siri can stay in the background. (Cmd+N keystroke is
        deliberately NOT used: it requires the app to be frontmost.)

        PATCH: returns whether the button was found and pressed; the caller
        fails closed when it wasn't.
        """
        app = ax.app_for_pid(pid)
        pressed = self._find_and_press(app, "New Conversation")
        time.sleep(0.6)
        return pressed

    @staticmethod
    def _open_new_conversation_menu(app) -> bool:
        """Recover a windowless app via the observed File menu command."""
        import ApplicationServices as a

        def read(ref, name):
            err, value = a.AXUIElementCopyAttributeValue(ref, name, None)
            if err in (a.kAXErrorAttributeUnsupported, a.kAXErrorNoValue):
                return None
            if err:
                raise base.CaptureError(f'menu {name} read failed ({err})')
            return value

        def children(ref):
            return list(read(ref, 'AXChildren') or [])

        bars = [n for n in children(app) if read(n, 'AXRole') == 'AXMenuBar']
        if len(bars) != 1:
            return False
        files = [n for n in children(bars[0]) if read(n, 'AXTitle') == 'File']
        if len(files) != 1:
            return False
        matches = [item for menu in children(files[0]) for item in children(menu)
                   if read(item, 'AXRole') == 'AXMenuItem'
                   and read(item, 'AXTitle') == 'New Conversation']
        if len(matches) != 1 or read(matches[0], 'AXEnabled') is not True:
            return False
        err, actions = a.AXUIElementCopyActionNames(matches[0], None)
        if err or 'AXPress' not in (actions or []):
            raise base.CaptureError('new conversation menu action unavailable')
        if a.AXUIElementPerformAction(matches[0], 'AXPress'):
            raise base.CaptureError('new conversation menu action failed; no retry')
        return True

    def _composer_ready(self, pid: int) -> bool:
        """True if the app has a composer we can type into."""
        app = ax.app_for_pid(pid)
        return self._find_composer(app) is not None

    def _reset_app(self) -> None:
        """Quit and relaunch the Siri AI app to clear a stuck/windowless state."""
        subprocess.run(
            ["osascript", "-e", 'tell application "Siri AI" to quit'],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(2)
        # Relaunch with `open -a` so a real window appears.
        subprocess.run(
            ["open", "-a", _SIRI_AI_APP], capture_output=True, text=True, timeout=5
        )
        time.sleep(self.open_delay_s)

    def _has_window(self, pid: int) -> bool:
        """True if the app has at least one AX window (i.e. is usable)."""
        import ApplicationServices as a

        app = a.AXUIElementCreateApplication(pid)
        return len(self._windows(app)) > 0

    def _focus_composer(self) -> None:
        """Ensure the composer text field has keyboard focus.

        Only meaningful in foreground mode; harmless otherwise.
        """
        pid = self._find_pid()
        if pid is None:
            return
        app = ax.app_for_pid(pid)
        field = self._find_composer(app)
        if field is not None:
            import ApplicationServices as a

            act_err, actions = a.AXUIElementCopyActionNames(field, None)
            if act_err == 0 and "AXFocus" in list(actions):
                a.AXUIElementPerformAction(field, "AXFocus")
        time.sleep(0.2)

    def _type_query_ax(self, query: str) -> None:
        """Type the query via the AX composer (set value + confirm).

        No keystroke fallback: an uncertain target or failed AX action aborts.
        """
        import ApplicationServices as a

        pid = self._find_pid()
        if pid is None:
            raise base.CaptureError("Siri AI disappeared before submission")
        _pane, composer = self._turn_container(pid)
        field = composer.ref
        if a.AXUIElementSetAttributeValue(field, "AXValue", query) != 0:
            raise base.CaptureError("could not set composer; no keystroke fallback")
        time.sleep(self.submit_delay_s)
        if a.AXUIElementPerformAction(field, "AXConfirm") != 0:
            raise base.CaptureError("query confirmation failed; submission uncertain")

    def _type_query(self, query: str) -> None:
        """Fallback: type via System Events keystroke (needs app frontmost)."""
        subprocess.run(
            ["osascript", "-e", f'tell application "System Events" to keystroke {query!r}'],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(self.submit_delay_s)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 36'],
            capture_output=True, text=True, timeout=5,
        )  # Return

    def _wait_for_response(self, pid: int, deadline: float, *, echo: str = "") -> Optional[str]:
        """Observe only this turn; never equate text stability with completion.

        Live macOS 27 exposes the same modelResponse group for progress and
        results, with no observed busy/terminal flag. Until a terminal signal
        is validated, ask() fails closed even when inspect_current_turn()
        exposes a rendered candidate. Do not reintroduce a timing-only exit.
        """
        saw_candidate = False
        while time.monotonic() < deadline:
            state = self.inspect_current_turn(pid, expected_query=echo)
            saw_candidate = saw_candidate or bool(state["text"])
            time.sleep(0.4)
        if saw_candidate:
            raise base.CaptureError(
                "completion unverified: current-turn candidate rendered, but "
                "Siri exposes no validated AX terminal signal; no answer emitted"
            )
        return None

    def _copy_response(self) -> Optional[str]:
        """Select the response and copy it to the clipboard, then read it.

        Focuses the conversation, Cmd+A to select all, Cmd+C to copy, then
        reads pbpaste. Returns None if the clipboard is empty or only
        contains UI chrome (no real response).
        """
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "a" using {command down}'],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(0.15)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "c" using {command down}'],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(0.2)
        out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        text = out.stdout.strip()
        if not text:
            return None
        # If the copied text is just UI chrome (sidebar header), it's not a
        # real response — return None so the caller treats it as a failure.
        chrome = {
            "New Conversation", "Search", "No Conversation Selected", "",
        }
        if text in chrome:
            return None
        return text

    def _find_composer(self, app) -> Optional[object]:
        """Return the composer AXTextField, or None."""
        import ApplicationServices as a

        def walk(el, depth=0):
            if depth > 20:
                return None
            err_r, role = a.AXUIElementCopyAttributeValue(el, "AXRole", None)
            if err_r == 0 and str(role) == "AXTextField":
                act_err, actions = a.AXUIElementCopyActionNames(el, None)
                if act_err == 0 and ("AXConfirm" in list(actions) or "AXFocus" in list(actions)):
                    return el
            err_c, ch = a.AXUIElementCopyAttributeValue(el, "AXChildren", None)
            if err_c == 0 and ch:
                for c in ch:
                    r = walk(c, depth + 1)
                    if r is not None:
                        return r
            return None

        return walk(app)

    def _find_and_press(self, el, label: str, depth: int = 0) -> bool:
        """Find an AXButton with the label and perform AXPress on it.

        Used as a reliable fallback to start a new conversation when the
        Cmd+N shortcut isn't accepted (e.g. app not fully focused).
        """
        import ApplicationServices as a

        if depth > 25:
            return False
        err_r, role = a.AXUIElementCopyAttributeValue(el, "AXRole", None)
        role = str(role) if err_r == 0 else ""
        if role == "AXButton":
            for attr in ("AXTitle", "AXValue", "AXDescription", "AXLabel"):
                err, val = a.AXUIElementCopyAttributeValue(el, attr, None)
                if err == 0 and val is not None and label.lower() in str(val).lower():
                    return a.AXUIElementPerformAction(el, "AXPress") == 0
        err_c, children = a.AXUIElementCopyAttributeValue(el, "AXChildren", None)
        if err_c == 0 and children is not None:
            for c in children:
                if self._find_and_press(c, label, depth + 1):
                    return True
        return False

    # -- response extraction (turn-scoped) --------------------------------

    def _turn_container(self, pid: int):
        """Snapshot the app AX tree and isolate the conversation container.

        Returns (pane_node, composer_node). Raises CaptureError for unknown
        topology, duplicate windows, or incomplete snapshots.
        """
        app = ax.app_for_pid(pid)
        windows = self._windows(app)
        if len(windows) != 1:
            raise base.CaptureError("expected exactly one Siri window")
        try:
            # Sidebar traversal needs structure only; never request its text
            # or payload fields while locating the current conversation pane.
            root = ax.snapshot_tree(windows[0], structural=True)
        except ValueError as exc:
            raise base.CaptureError(str(exc)) from exc
        composer = ax.find_composer_node(root)
        if composer is None:
            raise base.CaptureError("composer not found in AX tree")
        container = ax.conversation_container(root, composer)
        if container is None:
            raise base.CaptureError("conversation container unidentifiable")
        try:
            pane = ax.snapshot_tree(container.ref)
            scoped_composer = ax.find_composer_node(pane)
            from CoreFoundation import CFEqual
            if (not pane.complete or scoped_composer is None or
                    not CFEqual(composer.ref, scoped_composer.ref)):
                raise ValueError('composer identity changed while isolating the pane')
            ax.transcript_container(pane)
            if any(n.element.identifier in ('chatGridView', 'gridItemView') for n in ax.nodes(pane)):
                raise ValueError('sidebar appeared inside current pane')
        except ValueError as exc:
            raise base.CaptureError(str(exc)) from exc
        return pane, scoped_composer

    def inspect_current_turn(self, pid: int, *, expected_query: str = "") -> dict:
        """Read-only candidate inspection, explicitly NOT completion proof.

        modelResponse excludes the duplicated AXTextArea and userPrompt.
        The exact text and card labels survive; no prompt/content cleaner.
        """
        pane, _composer = self._turn_container(pid)
        transcript = ax.transcript_container(pane)
        prompts = [n for n in ax.nodes(transcript) if n.element.identifier == "userPrompt"]
        responses = [n for n in ax.nodes(transcript) if n.element.identifier == "modelResponse"]
        if len(prompts) > 1 or len(responses) > 1:
            raise base.CaptureError("multiple turns in current transcript")
        if expected_query and prompts:
            prompt = "\n".join(ax.extract_scoped_texts(prompts[0]))
            if _norm(prompt) != _norm(expected_query):
                raise base.CaptureError("current prompt changed; refusing readback")
        response = responses[0] if responses and prompts else None
        try:
            text = "\n".join(ax.extract_scoped_texts(response)) if response else None
        except ValueError as exc:
            raise base.CaptureError(str(exc)) from exc
        cards = []
        if response:
            for node in ax.nodes(response):
                el = node.element
                if (el.identifier or "").startswith("Identifier:ResultCell") or el.url or el.filename or el.document:
                    cards.append({
                        "role": el.role, "title": el.title, "value": el.value,
                        "description": el.description,
                        "labels": ax.extract_scoped_texts(node),
                        "url": el.url, "filename": el.filename,
                        "document": el.document, "identifier": el.identifier,
                        "bounds": el.bounds, "actions": el.actions,
                        "has_payload_attribute": bool(el.url or el.filename or el.document),
                        "attribute_errors": el.attribute_errors,
                    })
        return {"text": text or None, "completion": "unverified",
                "cards": cards, "response_bounds": response.element.bounds if response else None,
                "transcript_bounds": transcript.element.bounds}

    def _extract_turn_text(self, pid: int) -> Optional[str]:
        return self.inspect_current_turn(pid)["text"]

    def probe_result_cards(self, pid: int) -> list[dict]:
        """Include result cells without payloads; mark absence explicitly."""
        return self.inspect_current_turn(pid)["cards"]

    @staticmethod
    def _windows(app) -> list:
        """Return the app's AXWindow elements (excluding the menu bar)."""
        import ApplicationServices as a

        err, children = a.AXUIElementCopyAttributeValue(app, "AXChildren", None)
        if err != 0:
            raise base.CaptureError(f'AX application children read failed ({err})')
        if children is None:
            return []
        result = []
        for c in children:
            err_r, role = a.AXUIElementCopyAttributeValue(c, "AXRole", None)
            if err_r != 0:
                raise base.CaptureError(f'AX application child role read failed ({err_r})')
            if err_r == 0 and str(role) == "AXWindow":
                result.append(c)
        return result

    @staticmethod
    def _filter_conversation_text(texts: list[str]) -> Optional[str]:
        """Keep conversation content; drop chrome, menus, and duplicate lines.

        Also collapses the UI's repeated rendering (the query and answer
        often appear twice) and drops one-off button labels like "Copy".
        """
        noise_prefixes = (
            "About This Mac", "System Information", "System Settings…",
            "App Store", "Recent Items", "Applications",
            "Show “", "Clear Menu", "Force Quit", "Sleep", "Restart",
            "Shut Down", "Lock Screen", "Log Out", "About Siri",
        )
        noise_exact = {
            "Today", "Yesterday", "Previous 7 Days", "Previous 30 Days",
            "New Conversation", "Search", "0.0", "No Conversation Selected",
            "Copy", "Like", "Share", "Dismiss",
        }
        keep: list[str] = []
        for t in texts:
            s = " ".join(t.split())  # collapse whitespace
            if not s:
                continue
            if s in noise_exact:
                continue
            if s.startswith(noise_prefixes):
                continue
            # Ignore numeric-only lines (the app leaks progress/animation
            # values like "0.0063" into the AX tree).
            try:
                float(s)
                continue
            except ValueError:
                pass
            # Skip a line that's identical to the previous kept line
            # (the app often renders the query/answer twice).
            if keep and s == keep[-1]:
                continue
            keep.append(s)
        if not keep:
            return None
        return "\n".join(keep)
