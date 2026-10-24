"""Local-only structural probe. No screenshots, result activation or transport.

--approved-one-query must only be used after the owner says go.
"""
import argparse
import fcntl
import json
import threading
import time
import traceback
import uuid
from pathlib import Path

from siri_cli.preview_provider import (NativePreviewPort, Turn, scope_changes,
    allowed_placeholder_transition, apply_placeholder_handoff, observe_preview_turn)
from siri_cli.capture.transaction import SubmissionState
from siri_cli.relay import Incoming, Ticket, RelayFailure

QUERY = 'find my drawing of batteries connected in series'
REQUESTS = {'series': QUERY, 'transistor': 'find my transistor drawing',
            'caption-series': 'Show me the series connection diagram that I drew not too long ago'}


def describe_response(response, transcript_bounds, ax):
    """Read geometry/capability only, from an already scoped response."""
    from siri_cli.capture.current import contains
    rows = []
    def visit(node, path):
        if len(rows) >= 1000:
            raise RelayFailure('response_structure_too_large')
        bounds = ax._element_bounds(node.ref, reader=ax._get_attr_checked)
        rows.append({'path': path, 'role': node.element.role,
            'identifier': node.element.identifier, 'bounds': bounds,
            'actions': list(node.element.actions),
            'actions_error': node.element.actions_error,
            'fully_in_transcript': contains(transcript_bounds, bounds)})
        for i, child in enumerate(node.children):
            visit(child, path + [i])
    visit(response, [])
    for row in rows:
        row['fully_in_response'] = contains(rows[0]['bounds'], row['bounds'])
    return rows


def classify(rows):
    images = [r for r in rows if r['role'] == 'AXImage']
    grids = [r for r in rows if r['role'] in ('AXGrid', 'AXTable', 'AXCollection')]
    if len(images) > 1 or grids:
        return 'gallery_or_collection_requires_review'
    if len(images) == 1 and images[0]['fully_in_transcript'] and images[0]['fully_in_response']:
        return 'one_geometric_image_candidate_identity_and_pixels_unproved'
    if images:
        return 'image_geometry_unusable'
    return 'no_accessible_image_identified'


class StructuralPort(NativePreviewPort):
    def record_scope(self, identity, root, nodes, responses, transcript, ax):
        if not self.submitted:
            return
        names = ('pid', 'window', 'pane', 'composer', 'transcript', 'prompt', 'response')
        previous = getattr(self, 'diagnostic_identity', None)
        if not hasattr(self, 'diagnostic_refs'):
            self.diagnostic_refs = []
        def token(ref):
            if ref is None:
                return None
            for i, old in enumerate(self.diagnostic_refs):
                if same(old, ref):
                    return 'ref-' + str(i)
            self.diagnostic_refs.append(ref)
            return 'ref-' + str(len(self.diagnostic_refs)-1)
        descriptors = {}
        for name, node in nodes.items():
            if node is None:
                descriptors[name] = None
                continue
            path = ax._path_to(root, node)
            descriptors[name] = {'role': node.element.role, 'identifier': node.element.identifier,
                'path': [next(i for i, c in enumerate(a.children) if c is b)
                         for a, b in zip(path, path[1:])] if path else None,
                'bounds': ax._element_bounds(node.ref, reader=ax._get_attr_checked)}
        self.scope_snapshot = {'phase': 'scope_snapshot',
            'same': {n: same(a, b) for n, a, b in zip(names, previous, identity)} if previous else None,
            'identity': {n: (ref if n == 'pid' else token(ref)) for n, ref in zip(names, identity)},
            'nodes': descriptors, 'prompt_count': self.scope_observation['prompt_count'],
            'response_count': len(responses), 'eligibility': 'structural_diagnostic_only',
            'response_structure': describe_response(responses[0],
                ax._element_bounds(transcript.ref, reader=ax._get_attr_checked), ax) if len(responses) == 1 else []}
        self.diagnostic_identity = identity
        self.diagnostic_emit(self.scope_snapshot)

    def read_verified_text(self):
        # Read only already snapshotted nodes of the verified response reference.
        # Never walk or read text from a newly observed replacement.
        from siri_cli.capture import ax
        import ApplicationServices as A
        result = []
        for node in ax.nodes(self.response):
            if node.element.role not in ('AXStaticText', 'AXHeading', 'AXParagraph'):
                continue
            error, names = A.AXUIElementCopyAttributeNames(node.ref, None)
            if error:
                raise RelayFailure('response_text_attributes_failed')
            for attr in ('AXValue', 'AXTitle', 'AXDescription'):
                if attr in (names or ()):
                    value = ax._get_attr_checked(node.ref, attr)
                    if isinstance(value, str) and value:
                        result.append(value)
                        break
        return result

    def observe(self, ticket):
        from siri_cli.capture import ax
        from siri_cli.capture.deadline import bounded_ax_reads
        ticket.check()
        with bounded_ax_reads(ticket.deadline):
            if self.backend._find_pid() != self.pid:
                raise RelayFailure('siri_process_replaced')
            windows = self.backend._windows(ax.app_for_pid(self.pid))
            if len(windows) != 1:
                raise RelayFailure('unique_siri_window_required')
            root = ax.snapshot_tree(windows[0], structural=True)
            composer = ax.find_composer_node(root)
            if composer is None:
                raise RelayFailure('unique_composer_required')
            pane = ax.conversation_container(root, composer)
            if pane is None:
                raise RelayFailure('unique_pane_required')
            transcript = ax.transcript_container(pane)
            prompts = [n for n in ax.nodes(transcript) if n.element.identifier == 'userPrompt']
            responses = [n for n in ax.nodes(transcript) if n.element.identifier == 'modelResponse']
            identity = (self.pid, windows[0], pane.ref, composer.ref, transcript.ref,
                prompts[0].ref if len(prompts) == 1 else None,
                responses[0].ref if len(responses) == 1 else None)
            def descriptor(node):
                path = ax._path_to(root, node)
                indices = [next(i for i, c in enumerate(a.children) if c is b)
                    for a, b in zip(path, path[1:])] if path else None
                return {'role': node.element.role, 'identifier': node.element.identifier,
                        'path': indices}
            self.scope_observation = {'phase': 'after_submit' if self.submitted else 'before_submit',
                'same': scope_changes(self.base, identity) if hasattr(self, 'base') else None,
                'nodes': {'pane': descriptor(pane), 'composer': descriptor(composer),
                          'transcript': descriptor(transcript)},
                'raw_children': len(transcript.children),
                'prompt_count': len(prompts), 'response_count': len(responses)}
            self.record_scope(identity, root, {'window': root, 'pane': pane, 'composer': composer,
                'transcript': transcript, 'prompt': prompts[0] if len(prompts) == 1 else None,
                'response': responses[0] if len(responses) == 1 else None}, responses, transcript, ax)
            if not hasattr(self, 'scope_baseline'):
                self.scope_baseline = self.scope_observation
            handoff = None
            if hasattr(self, 'base') and not all(self.scope_observation['same'].values()):
                if (self.submitted and not getattr(self, 'handoff_used', False) and
                    allowed_placeholder_transition(self.scope_baseline, self.scope_observation)):
                    handoff = {'old_identity': self.base, 'before': self.scope_baseline,
                               'after': self.scope_observation}
                    self.handoff_used = True
                else:
                    raise RelayFailure('siri_scope_replaced')
            self.base = identity
            value = ax._get_attr_checked(composer.ref, 'AXValue') or ''
            texts = ['\n'.join(ax.extract_scoped_texts(ax.snapshot_tree(n.ref)))
                     for n in prompts] if self.submitted and len(prompts) == 1 else []
            self.response = responses[0] if len(responses) == 1 else None
            self.transcript = transcript
            # "ready" here pins a response identity only; never completion.
            has_image = self.response is not None and any(n.element.role == 'AXImage'
                                                         for n in ax.nodes(self.response))
            return Turn(identity, value, texts, len(responses), len(transcript.children),
                        bool(has_image), handoff=handoff)

    def capture(self, *args):
        raise RelayFailure('structural_probe_capture_forbidden')


class ExistingEmptyPort(StructuralPort):
    """Record-only variant: never create a conversation to replace a changed pane."""
    def prepare_empty(self, ticket):
        before = self.observe(ticket)
        if before.composer or before.raw_children or before.prompts or before.response_count:
            raise RelayFailure('existing_empty_pane_required_no_menu_action')
        return before


from siri_cli.preparation import StalePreparationMixin


class StalePreparationPort(StalePreparationMixin, StructuralPort):
    pass


def probe(ticket, port, emit, *, observation_seconds=60):
    from siri_cli.capture import ax
    from siri_cli.capture.deadline import bounded_ax_reads
    port.diagnostic_emit = emit
    before = port.prepare(ticket)
    if before.raw_children or before.prompts or before.response_count or before.composer:
        raise RelayFailure('fresh_empty_turn_required')
    query = ticket.incoming.query
    state = SubmissionState(ticket.request_id, query, binding=before.identity)
    emit({'phase': 'fresh_turn_bound'})
    emit({'phase': 'submission_attempted'})
    try:
        port.submit(ticket, before)
    except Exception as exc:
        raise RelayFailure('submission_uncertain_no_retry') from exc
    stop = None
    previous = None
    while True:
        ticket.check()
        turn = port.observe(ticket)
        if turn.handoff is not None:
            apply_placeholder_handoff(state, turn)
        observe_preview_turn(state, turn)
        if state.acknowledged:
            if turn.prompts != [query]:
                raise RelayFailure('exact_prompt_mismatch')
            if stop is None:
                emit({'phase': 'exact_prompt_acknowledged'})
                stop = min(ticket.deadline, time.monotonic() + observation_seconds)
            if port.response is not None:
                before_scope = getattr(port, 'scope_snapshot', None)
                with bounded_ax_reads(ticket.deadline):
                    bounds = ax._element_bounds(port.transcript.ref, reader=ax._get_attr_checked)
                    rows = describe_response(port.response, bounds, ax)
                # Recheck identity before emitting the scoped structure.
                after = port.observe(ticket)
                emit({'phase': 'structure_read_pair', 'before': before_scope,
                      'after': getattr(port, 'scope_snapshot', None),
                      'same': {n: same(a, b) for n, a, b in zip(
                          ('pid','window','pane','composer','transcript','prompt','response'),
                          turn.identity, after.identity)}})
                if after.handoff is not None:
                    raise RelayFailure('late_scope_handoff')
                observe_preview_turn(state, after)
                if after.prompts != [query]:
                    raise RelayFailure('exact_prompt_mismatch')
                if any(not same(a, b) for a, b in zip(turn.identity, after.identity)):
                    raise RelayFailure('response_changed_during_structure_read')
                if rows != previous:
                    emit({'phase': 'current_response_structure', 'classification': classify(rows),
                          'nodes': rows, 'completion': 'unverified'})
                    previous = rows
                # Identity has just passed both the state and pair checks.
                with bounded_ax_reads(ticket.deadline):
                    text = getattr(port, 'read_verified_text', lambda: [])()
                checked = port.observe(ticket)
                emit({'phase': 'text_read_identity_check', 'same': {n: same(a,b) for n,a,b in zip(
                    ('pid','window','pane','composer','transcript','prompt','response'),
                    after.identity, checked.identity)}})
                if checked.handoff is not None:
                    raise RelayFailure('late_scope_handoff')
                observe_preview_turn(state, checked)
                if checked.prompts != [query] or any(not same(a,b) for a,b in zip(after.identity, checked.identity)):
                    raise RelayFailure('response_changed_during_text_read_text_discarded')
                if text:
                    emit({'phase': 'verified_current_response_text', 'text': text,
                          'completion': 'unverified'})
            if time.monotonic() >= stop:
                return {'phase': 'observation_ended', 'classification': classify(previous or []),
                        'completion': 'unverified', 'pixel_identity': 'unproved'}
        time.sleep(min(.5, max(0, ticket.deadline - time.monotonic())))


def same(a, b):
    if a is None or b is None:
        return a is b
    from siri_cli.capture.transaction import _same_ref
    return _same_ref(a, b)


def failure_event(exc):
    from siri_cli.capture.ax import AXReadError
    event = {'phase': 'aborted', 'error_type': type(exc).__name__,
             'reason': str(exc) if isinstance(exc, RelayFailure) else 'structural_read_or_identity_failure',
             'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno,
                         'function': frame.name} for frame in traceback.extract_tb(exc.__traceback__)],
             'retry': False}
    if isinstance(exc, AXReadError):
        event['ax_failure'] = {'operation': exc.operation, 'error_code': exc.error_code,
            'structural_path': list(exc.structural_path) if exc.structural_path is not None else None,
            'path_origin': 'snapshot_root', 'node_attributes_required_for_path': False}
    return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--approved-one-query', action='store_true')
    parser.add_argument('--require-existing-empty', action='store_true')
    parser.add_argument('--allow-stale-preparation-read', action='store_true')
    parser.add_argument('--request', choices=tuple(REQUESTS), default='series')
    args = parser.parse_args()
    if not args.approved_one_query:
        parser.error('owner go-ahead required; no query was submitted')
    if args.require_existing_empty and args.allow_stale_preparation_read:
        parser.error('choose one preparation policy')
    output = Path(args.output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.monotonic()
    query = REQUESTS[args.request]
    ticket = Ticket(str(uuid.uuid4()), Incoming('local-drawing-probe', 'local-only', query),
                    output, started + 120, threading.Event())
    def emit(event):
        with (output / 'structure.jsonl').open('a') as stream:
            stream.write(json.dumps({'elapsed_s': time.monotonic() - started,
                                     'run': ticket.request_id, **event}) + '\n')
    with open('/tmp/siri-cli-current-turn.lock', 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            emit({'phase': 'started', 'query': query})
            port_type = (ExistingEmptyPort if args.require_existing_empty else
                         StalePreparationPort if args.allow_stale_preparation_read else StructuralPort)
            emit(probe(ticket, port_type(live_enabled=True), emit))
        except Exception as exc:
            emit(failure_event(exc))
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
