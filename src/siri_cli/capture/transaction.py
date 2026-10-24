"""Submission acknowledgement only; no state here proves answer completion."""
from dataclasses import dataclass


def _same_ref(a,b):
    if a is b:return True
    from CoreFoundation import CFEqual
    return bool(CFEqual(a,b))


@dataclass
class SubmissionState:
    run_id: str
    query: str
    acknowledged: bool = False
    composer_cleared: bool = False
    # PID, window, pane, composer, transcript, prompt, response. Established
    # only on the empty transcript before submission; the last two start empty.
    binding: tuple | None = None

    def __post_init__(self):
        if self.binding is not None:
            if len(self.binding)!=7 or any(x is None for x in self.binding[:5]) or self.binding[5:]!=(None,None):
                raise ValueError('submission binding must precede prompt and response')

    def _check_identity(self, identity, prompts, response_count):
        if self.binding is None:
            if identity is not None:raise ValueError('cannot establish provenance after submission')
            return
        if identity is None or len(identity)!=7 or any(x is None for x in identity[:5]):
            raise ValueError('submission identity unavailable')
        if bool(identity[5] is not None)!=bool(prompts) or bool(identity[6] is not None)!=bool(response_count):
            raise ValueError('submission identity disagrees with turn structure')
        if self.binding[0]!=identity[0] or not all(_same_ref(a,b) for a,b in zip(self.binding[1:5],identity[1:5])):
            raise ValueError('submission owner or scope replaced')
        for old,new in zip(self.binding[5:],identity[5:]):
            if old is not None and (new is None or not _same_ref(old,new)):
                raise ValueError('bound prompt or response replaced or disappeared')
        self.binding=identity

    def observe(self, composer_value, prompts, response_count, *, identity=None):
        if response_count<0:raise ValueError('invalid response count')
        if len(prompts) > 1 or response_count > 1:
            raise ValueError('multiple turns after submission')
        self._check_identity(identity,prompts,response_count)
        if prompts and ' '.join(prompts[0].split()) != ' '.join(self.query.split()):
            raise ValueError('prompt changed after submission')
        value = composer_value or ''
        if value:
            if self.composer_cleared or value != self.query:
                raise ValueError('composer edited after submission')
        else:
            self.composer_cleared = True
        if self.acknowledged and not prompts:
            raise ValueError('acknowledged prompt disappeared')
        if response_count and not prompts and self.binding is None:
            raise ValueError('orphan response after submission')
        if prompts and self.composer_cleared:
            self.acknowledged = True
        return 'generating' if self.acknowledged else 'awaiting_acknowledgement'
