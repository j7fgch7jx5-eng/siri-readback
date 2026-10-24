"""Shared bounded preparation policy; never retries a submitted query."""
from .relay import RelayFailure


class StalePreparationMixin:
    """One complete root reacquisition after an invalid element, before submission only."""
    def prepare_empty(self, ticket):
        from .capture import ax
        from .capture.deadline import bounded_ax_reads
        from .capture.transaction import _same_ref as same
        import ApplicationServices as A
        emit = getattr(self, 'preparation_audit', getattr(self, 'diagnostic_emit', lambda event: None))
        original = self.backend._open_new_conversation_menu
        attempted = False
        anchor = None
        def menu_once(app):
            nonlocal attempted, anchor
            if attempted:
                raise RelayFailure('second_menu_action_forbidden')
            attempted = True
            anchor = self.base[:2]
            return original(app)
        self.backend._open_new_conversation_menu = menu_once
        try:
            return super().prepare_empty(ticket)
        except ax.AXReadError as error:
            if (error.error_code != A.kAXErrorInvalidUIElement or not attempted or
                anchor is None or self.submitted):
                raise
            emit({'phase': 'preparation_invalid_element',
                'ax_failure': ax.ax_read_failure(error), 'reacquisition_limit': 1})
            ticket.check()
            with bounded_ax_reads(ticket.deadline):
                if self.backend._find_pid() != anchor[0]:
                    raise RelayFailure('preparation_process_replaced')
                windows = self.backend._windows(ax.app_for_pid(anchor[0]))
                if len(windows) != 1 or not same(windows[0], anchor[1]):
                    raise RelayFailure('preparation_window_replaced')
                # Discard all failed-tree bindings. No node is skipped and no
                # partial snapshot is returned. A second read failure propagates.
                if hasattr(self, 'base'):
                    del self.base
                current = self.observe(ticket)
            ticket.check()
            if (current.identity[0] != anchor[0] or not same(current.identity[1], anchor[1]) or
                current.composer or current.raw_children or current.prompts or current.response_count):
                raise RelayFailure('reacquired_preparation_not_empty')
            emit({'phase': 'preparation_reacquired_empty',
                                  'same_process': True, 'same_window': True})
            return current
        finally:
            self.backend._open_new_conversation_menu = original

