# Pro Pipeline — implementation gates

## Verified extension behavior

Source: local Seedance Studio Pro/content.js, executeBatchSession.
Pro mode chunks two prompts, formats each and joins them with a newline into
ONE message. It waits for preliminary outcome, then 30 seconds before opening
another chat. It is not two independently acknowledged requests 30 seconds apart.
Do not map two gateway tasks to the first video in that conversation.

## Current repair

- Explicit New Chat action before model selection; require /chat and empty editor.
- Reject previous conversation ID and require full normalized prompt equality.
- Unknown submission raises a separate error, without automatic resubmission or
  treating it as a render timeout in the local usage counter.
- Retain one task per conversation and account isolation.
- Real test deliberately starts from acc6's old OK conversation before opening
  New Chat, submitting a 15s Pro video, verifying a new ID and downloading.

## Remaining gates (not yet implemented or enabled)

1. Persist verified upstream message/request/creation identity and submission
   timestamps. Inspect actual upstream message linkage, including duplicate
   prompts and multiple creations; do not infer correspondence from array order.
2. Introduce an account-owned session coordinator with serialized UI submission,
   bounded active jobs and independent polling. Cover cancellation, login/verify,
   shutdown, crash recovery, quota and cooldown; retain profile exclusivity.
3. Start with one prompt per new chat and one active job/account. Measure cold
   versus reused-browser preparation and total batch time on real accounts.
4. Only after isolation/recovery tests pass, allow two active jobs/account with
   a minimum 30s spacing after confirmed submission. Disable immediately on
   captcha, unknown submission or quota errors.
5. Two prompts in one chat remains disabled until upstream identities can map
   every output reliably, even for identical prompts and reversed completion.

## Acceptance evidence required

Record each test's account, conversation ID, submit latency, result latency,
output file, duration verification and outcome. Compare equal batches; do not
claim reduced rendering latency based on faster preparation alone. Mocked tests
are useful for race conditions but do not substitute for live generation.

## Evidence collected 2026-09-12

- acc6 integration probe started deliberately from old conversation
  38417525216489489, explicitly opened New Chat and submitted a new 15s Pro scene.
- New verified conversation: 38417892582259985, after 9.106 seconds.
- Completed/downloaded after 250.187 seconds; output:
  downloads/acc6_20260912_190946.mp4.
- OpenCV decoded the output: 361 frames, 24 fps, 15.0417 seconds, 720x1280.
- acc5 was separately reset from an existing chat three times, all reached /chat;
  full prompt matching against an actual completed acc5 conversation succeeded.
- 25 backend tests and two Chromium Batch UI tests passed.
- Server restarted with the repair and returned healthy, zero pending tasks.
- This is evidence for the repair only, NOT for two jobs/account, crash recovery
  under pipeline mode, or reduced total batch time. Those remain unimplemented.
