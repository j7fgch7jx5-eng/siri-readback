# Siri Readback

A human-reviewed local reader built on [siri-cli by Pasithea](https://github.com/Pasithea0/siri-cli). It submits one request to the Mac's Siri and returns attributable text or a readable document for review.

## What works locally

- Plain, calculator and supported cloud text responses, preserving the exact answer text.
- Verified resident/windowless startup through one normal app open/reopen.
- A request-bound document-share Copy route: PDF bytes are validated; HEIC/JPEG/PNG originals are decoded and converted locally to a one-page PDF. The current payload can be reviewed without screenshot OCR.
- Correct refusal handling on fresh responses, cached replay and review. Recognized historical refusals are failures; original journals are preserved.
- Durable request IDs, verified cache replay, identity/bounds/obstruction checks, cancellation, owned-preview cleanup and persistent protection against overlapping or uncertain submissions.

This remains an early reviewed prototype, not an installer or a public service. It requires a Mac with Siri AI and the relevant macOS authorization. Actual local text and drawing readback passed live checks; those private requests, answers and documents are not in this repository. See [VALIDATION.md](VALIDATION.md) for sanitized results and limits.

## Local use

Use a disposable Python environment on macOS and the dependencies declared in `pyproject.toml`. For example, from the checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
./readback --query 'YOUR REQUEST' --expect text --text
```

For a document request, use `--expect document` instead of `--expect text --text`. Its result includes a run ID and private PDF path. Inspect the exact saved output:

```sh
./review readback-runs/RUN_ID
```

Open the printed PDF and inspect its actual content. Nothing is sent or approved automatically. The Mac must be idle and unlocked; user activity stops GUI input. The launcher uses the active `python3`; `SIRI_READBACK_PYTHON` and `SIRI_READBACK_VALIDATOR` can select existing runtimes.

Outputs go to `readback-runs/` beside the launchers. A new call gets a new run ID. `--run-id NAME` and `--output PRIVATE_FOLDER` make the identity/location explicit. Reusing a successful ID checks and returns its cache without another Siri query. Failed or uncertain IDs cannot be automatically resubmitted. Keep both `readback-runs/` and `local-state/`; neither belongs in Git.

## Failure and waiting policy

On an explicit failure the command exits 1 and prints:

> Siri failed. Text me again to retry.

That attempt ends. There is no retry button, follow-up question, runtime investigation/repair loop or automatic resubmission. A subsequent request is a new attempt.

The default response window is **180 seconds**, with a separate bounded startup/idle/artifact margin. `--timeout 240` gives a longer response budget; fresh windows support 120–600 seconds. A slow or uncertain response is pending, not an explicit error. A pending result exits 2 and persistently prevents another submission, even after restart or a new ID. Separate operator reconciliation is required; the finished runtime does not investigate or clear the hold itself.

`--expect text` does not wait for file bytes. `--expect document` requires a validated artifact rather than accepting a text preamble. Default `auto` returns the first supported readable type and may return text before a later card. `--diagnostics` enables developer-only structured failure output; it does not enable retries or repair.

## Limits and transport

Fully PID-absent cold startup is covered offline; the live startup proof is the resident/windowless state. Unsupported or ambiguous layouts continue to fail safely. Source controls can lack accessible labels or URLs. Exact text and actual document review remain mandatory.

Native Siri generation completion is **unverified**. A readable preview or successful Copy does not prove completion, and strict `ask()` retains its separate blocked completion requirement. An older disabled native PDF-card route remains distinct from the working HEIC-to-PDF route.

The intended broader concept is to text a contact named Siri and receive the Mac's Siri result in that conversation. **Automatic messaging transport is not finished or enabled by this reader.** Generic transport adapters remain supplied as separately gated source/offline fixtures; no account, recipient, credentials, running receiver or delivery service is configured here.

## Offline tests

With the development dependencies installed, run only the offline suite:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests:. python -m pytest -p no:cacheprovider tests -q
```

Node.js is required for the existing generic service-adapter tests. Fixtures use synthetic content and fake senders. These tests do not invoke Siri, start a listener or send messages. Live use is opt-in; no live diagnostic is run by the test command.

## License

MIT. The original Pasithea copyright and the fork's anonymous-account notice are preserved in [LICENSE](LICENSE). This repository preserves the existing `main` history of `j7fgch7jx5-eng/siri-readback`.
