"""Finished-workflow messages and exact observed source-error recognition."""
FAILURE_MESSAGE='Siri failed. Text me again to retry.'
PENDING_MESSAGE='Siri is still pending. This request was not retried.'
SOURCE_ERRORS=frozenset((
    'Sorry, I don’t understand.',"Sorry, I don't understand.",
    'I’m having a bit of trouble here. Please try again.',
    "I'm having a bit of trouble here. Please try again.",
))


def source_error(text):
    # Exact whole-answer match. Do not normalize, clean or truncate answers.
    return isinstance(text,str) and text in SOURCE_ERRORS
