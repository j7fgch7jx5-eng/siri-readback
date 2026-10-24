# Public staging review

Prepared for the proposed `j7fgch7jx5-eng/siri-readback` repository. This is a local staging copy only: no Git repository was initialized, no remote was added, and nothing was pushed or published.

## Contents and attribution

The MIT license preserves `Copyright (c) 2026 Pasithea` and adds `Copyright (c) 2026 j7fgch7jx5-eng`. The README describes a human-reviewed early prototype and explicitly states that the latest live local readback did not capture because of a bounds guard. It does not claim unattended automatic operation, an installer, or a public service.

Source and offline tests were copied, not moved. Matching upstream tests are under `tests/upstream`. Generic transport code is included without a working deployment or credentials. The installation and original working sources were not edited.

## Sanitization in this copy

- Replaced the private destination in bridge tests with an opaque fixture line.
- Replaced test login/contact strings with opaque fixture identities.
- Changed the test-only signing material to ephemeral bytes generated during tests; no signing material was copied into this repository.
- Removed private evidence references from structural fixture metadata and gave the fixtures generic names. Fixtures contain only roles, geometry and boolean checks, never personal text or image bytes.
- Rewrote a long numeric mock deadline as an arithmetic expression with the same value to eliminate a phone-pattern false positive.
- Created independent fork package metadata and a fork version; the working package was not changed.

## Files deliberately withheld

No credentials, environment files, real service identifiers, phone numbers, message history, personal captures, logs, runtime databases, caches, installed dependencies, prior archives or private reports were copied. Host-bound Photon launch/receive/send scripts and the private request plan were withheld. The SDK/harness-specific `tests/test_drawing_review_offline.py` was withheld because it requires those private entrypoints. Generic service-port tests remain in `tests/test_photon_bridge.py`.

## Scan results

The final staged tree was searched with case-insensitive content patterns and a broad credential/identity keyword pass. Literal-pattern findings after sanitation:

| Category | Matches |
| --- | ---: |
| credential-like literals | 0 |
| phone-like digit sequences | 0 |
| email addresses | 0 |
| absolute personal home paths | 0 |
| service message or project identifiers | 0 |
| known private identity markers | 0 |

The broad keyword pass still finds cryptographic parameter names, request-idempotency tokens, database column names and the generic Photon adapter name. These were reviewed as code vocabulary, not embedded credentials or real service identities. Synthetic sender names and generated signing bytes are test data. No unresolved secret or personal-data finding remains in this review; pattern scans are not an exhaustive security audit.

## Offline verification

**291 tests passed in 3.04 seconds** in the staged copy using existing dependencies read-only. Caching and bytecode writes were disabled. No live SDK, listener, Siri query or send was invoked by this suite. The omitted private harness wrapper accounts for the one-test difference from the working project's 292-test suite.

## Uncertain files

None included with unresolved provenance or privacy uncertainty. Environment-bound deployment files were excluded rather than guessed safe. `src/siri_cli/messages_live.py` and `diagnostics/photon/service_port.mjs` are generic, explicitly gated transport source, not supplied credentials or enabled deployment configuration. They still require a human code review before anyone configures live use.

See `FILES.txt` for the complete staged file list. The earlier working-project evidence and installed setup remain outside this folder.
