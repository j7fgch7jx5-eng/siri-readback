# Public source review

This update targets the existing public `j7fgch7jx5-eng/siri-readback` main branch and preserves its history and MIT/Pasithea attribution. Author and committer use only the repository owner's existing anonymous account and GitHub noreply identity.

## Included and withheld

Included: reviewed reader source, portable launchers, offline regression tests, synthetic structural fixtures, package metadata, and sanitized usage/validation notes. Existing generic transport source remains gated; no deployment or automatic delivery is enabled.

Withheld: credentials, environment files, personal paths/contacts/account identifiers, private requests/answers, journals, uncertainty state, actual documents/images, screenshots, logs, backups, private reports and delivery archives. No directory containing private evidence or output was traversed for copying. Demonstration-specific literals retained in older tracked diagnostics/fixtures were replaced with synthetic examples in the current tree. Existing main history was not rewritten.

Generic runtime parameter names, synthetic fixture identities, standard system paths and exact built-in Siri refusal sentences are code/test vocabulary, not credentials or recorded personal conversations. The anonymous repository namespace and required MIT fork notice are intentionally retained.

## Portability changes

The launchers use the active Python environment, with the existing explicit override supported. PDF parsing is a declared dependency; the private host-runtime cache fallback was removed. Synthetic PDF generation is a development dependency. No dependency was installed on the publishing host. The private delivery and installed setup remain untouched.

## Verification

The public checkout's own complete offline suite passed: **466 tests in 8.98 seconds**, using existing dependencies read-only, with bytecode/cache writes disabled. Tests used fixtures/fake senders; no Siri query, message body read, receiver or external send ran. Local private live validation is summarized separately in VALIDATION.md and is not substituted for the public test count.

The complete staged file inventory and diff are reviewed before commit. The staged/committed tree is scanned for credential literals, phone/email patterns, personal home paths, private identity markers, real project/message identifiers, actual recorded requests/answers and prohibited artifact file types. Synthetic cryptographic material is generated at test time. Standard PDF cross-reference zero padding and generic lock/system paths are reviewed structural data, not personal data.

See PUBLIC_SCAN.md for final scan categories/results and FILES.txt for the published file list. No unresolved private-data finding is included. Pattern scans are not an exhaustive security audit.
