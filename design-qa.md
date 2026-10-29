# Product Design QA — clean Hermes bot responses

final result: passed

## Comparison target
Existing Hermes native UI, intentionally changing only bot prose to remove gray boxes. Source visual: clean-prose-before-chat.jpg. Implementation: clean-prose-after-chat.jpg. Both are actual CEIA canonical Bot Chat captures, same 3600x2260 pixel native window, 2x density, dark Grok Chat skin. Both normalized to the same 1970x2130 crop at x1080/y75 and inspected together in one comparison input. The same settled review response, table and changed-file card are present; native scroll anchoring shifts the code panel after prose height changes. This is an intended result, not capture drift.

## Findings and fixes
- Resolved P1: runtime grok-chat-look-styles injects important gray prose-pill styling after the renderer CSS. Earlier selectors tied its specificity. Surface-qualified selector now outranks it for direct assistant prose only.
- Post-fix evidence: gray container, border, rounding and padding removed from the same settled reply. Text reads directly on the chat background. Existing code/diff panel, tool summaries, table boundary and changed-file card remain distinct. No substantive P0/P1/P2 issue remains in the scoped change.

## Required fidelity surfaces
- Fonts/typography: existing family, weight hierarchy, size and emphasis retained; wider unboxed text wraps naturally and remains readable.
- Spacing/layout: same header and composer; intentional removal of bubble inset and 88% pill cap uses the established reading column. No controls clipped.
- Colors/tokens: reply inherits existing foreground/background; only the large gray fill is removed. Semantic code/diff color and separate result-card fill retained.
- Image quality: original painted persona and crown/other avatar assets retained without regeneration or crop changes.
- Copy/content: same reply and table; no conversation text changed or test prompt sent.

## Verification
Production build, CSS formatting/diff checks, ASAR integrity and codesign --verify --deep --strict passed. Native app restarted, reconnected and CEIA canonical chat opened read-only. No approvals or file actions executed. Installed renderer commit b8535b47a6ecadceb5fc59793d332ffb0ed6d9f3. ASAR header SHA256 12cbd1c0c5c42a507a8171d642a67562f32091742c107561094b1d417367a582.

## Evidence
All captures local-only under /Users/matheuskindrazki/.codex/visualizations/2026/10/11/hermes-avatars/.
Before: clean-prose-before-chat.jpg
After: clean-prose-after-chat.jpg
Focused proof: clean-prose-proof.jpg
Backup: /Users/matheuskindrazki/.hermes/backups/clean-prose-20261012/Hermes.app

## Residual gaps
Light theme and narrow panes were not separately captured; this is a scoped background/inset change, not a layout or interaction redesign. No new P3 refinement required for this request. Local installation only, no upstream release claimed.

## Kindra v0.21.7 native install — 2026-10-12

- Ported the personalized fork to official stable v0.21.7; retained the original fork and recoverable installed-app backups.
- Native macOS menu and About panel say Kindra 0.21.7. Client and backend versions remain distinct; backend remains 0.20.6.
- Computer Use opens from Add context. No inline composer control overlaps the placeholder.
- Checked the readiness dialog and local @Slack suggestion labeled kindrinha.local without granting access or sending messages. Cleared the test draft.
- Renderer/electron types, focused UI and native tests, packaging and deep strict signing passed.
- Separate runtime repair: expired voice attempt was cleared using the controller's existing guarded expiry operation and CAS, after native writer inspection. No session resumed and no iPhone build changed.
- Native verification receipt: /Users/matheuskindrazki/.codex/visualizations/2026/10/11/hermes-avatars/kindra-v0217-installed-receipt.json
