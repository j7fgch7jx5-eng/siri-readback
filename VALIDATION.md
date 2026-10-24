# Sanitized validation summary

## Verified local behavior

The reviewed final local build returned a fresh non-calculator informational answer in **5.12 seconds**. A separate fresh run from the process-resident/windowless starting state opened the conversation, recovered the intended image-original payload, converted and validated a readable PDF, and confirmed owned-preview cleanup in **20.32 seconds**.

The actual PDF was parsed, rendered and visually inspected privately. The request, answer, image, PDF, run identifiers, journals, account/recipient identities and local filesystem paths are deliberately excluded from this public repository. The document route was HEIC-to-PDF; it was not evidence that a separate older disabled PDF card had become recoverable.

These passes establish those bounded reviewed paths, not unattended or universal reliability. Fully PID-absent launch was tested offline; ordinary resident/windowless reopening was demonstrated live.

## Failures found and corrected

Two development startup attempts ended conclusively before submission, each with the simple failure message. The defect was using activation/menu actions instead of the normal app reopen. The final startup path performs a single normal open/reopen and waits within its bound; it does not investigate or reset the app.

A later development attempt exposed cloud text with citation controls and an empty custom-view placeholder. The helper rejected the unsupported layout and kept an uncertainty hold. Development used a separately verified existing-only read to resolve the hold before making a new attempt. The failed journal was preserved and was never promoted into fresh success. The corrected fresh text route then passed.

A recognized historical refusal was checked through both review and replay: failure exit status, empty success output, exact simple failure message, no Siri query, and unchanged original journal bytes. Valid cached document replay succeeded without another submission. Reusing the unresolved historical run ID remained refused.

## Waiting and protection

The configured response window is 180 seconds after submission, subject to a bounded overall transaction deadline that includes preparation/artifact margin. Clock-driven offline coverage shows unchanged progress lasting 140 seconds can still succeed. A deadline with an uncertain submitted response remains pending across restart and blocks a new ID. A minute-plus live response was not forced or observed in this check.

Exact observed refusal strings are recognized, not arbitrary natural-language error detection. The runtime has no automatic retry, retry button, follow-up question, investigation or repair loop. Operator reconciliation during development is not a shipped runtime retry feature.

## Publication-specific verification

Public-source changes from the local build are portability/privacy changes: launchers use the active Python environment; PDF validation uses a declared dependency or explicit runtime override rather than a host's private runtime cache; demonstration-specific strings are replaced with synthetic fixtures. MIT/upstream attribution is retained.

The public checkout's own offline test and staged-tree scan results are recorded in PUBLIC_REVIEW.md. The private local suite's larger count is not represented as the public checkout's count. No private validation logs or captured payloads are published.

## Deliberate limits

Native completion remains unverified; strict ask() stays blocked. Automatic messaging transport is not completed or enabled. Unsupported/clipped/ambiguous content is not accepted merely to obtain a pass. Human review of exact text and the actual document remains necessary. No live recipient, project binding, credentials or deployment is supplied.
