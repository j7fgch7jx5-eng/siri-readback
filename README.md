# Siri Readback

A human-reviewed prototype for this flow: text a contact named **Siri** in iMessage, let the Mac's Siri answer, and read back the matching text and a readable result image in the conversation.

## Status

Early prototype, **not an installer or a public service**. It needs a Mac with **Siri AI**, the relevant macOS permissions, and separately configured message transport. This repository contains the readback source, generic relay/transport adapters and offline tests; personal deployment wiring and credentials are deliberately absent. Nothing listens or sends merely by importing the package.

The latest live local readback test **did not capture a result**: a bounds guard rejected an image extending beyond Siri's reported response container. Earlier reviewed demonstrations do not establish reliable automatic operation. Do not assume unattended use works. Photo replies still require review of the exact text and actual image; native Siri generation completion is not proved. The strict text API retains its separate completion requirement.

Fresh-turn identity, history exclusion, crop containment, deadlines and uncertain-send handling are intentional safeguards. A failed guard is not permission to reuse an old answer, retry an uncertain submission, or send conversation history.

## Development

Use your own disposable Python environment on macOS with the dependencies in `pyproject.toml`. Node.js is needed for the generic service-adapter tests. To run only the offline suite from this directory:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests:. python3 -m pytest -p no:cacheprovider tests -q
```

These tests use fixtures and fake senders; they do not constitute live acceptance. Live diagnostics are opt-in and must only be used with the owner's explicit approval. No live configuration or running relay is supplied. This README is not an installation guide.

## Attribution and license

Built on [siri-cli by Pasithea](https://github.com/Pasithea0/siri-cli), based on version 1.1.0. This fork is prepared for `j7fgch7jx5-eng/siri-readback`. Licensed under MIT; the original Pasithea copyright notice is preserved alongside the fork notice in [LICENSE](LICENSE).
