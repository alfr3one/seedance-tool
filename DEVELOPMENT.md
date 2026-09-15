# Video Generation Gateway - Architecture & Technical Notes

Technical reference for the service architecture, profile coordination, and task pipeline.

---

## 1. Core Architecture

The system operates across three tiers:
1. **API Tier (`server.py`)**: FastAPI application exposing standard OpenAI video endpoints (`/v1/videos/generations`, `/v1/videos/<id>`) and web dashboard routes (`/web`, `/api/admin/*`).
2. **Pool Management Tier (`browser_pool.py`)**: Manages persistent browser profiles in `accounts/`, controlling task concurrency, account locking, and daily quota rotations.
3. **Execution Tier (`video_worker_ui.py`, `video_worker.py`)**:
   - **UI Automation Mode**: Automates browser interactions, authenticates via persistent browser session, injects prompts, and handles verification challenges.
   - **Protocol Mode**: Sends structured SSE requests to completion endpoints and polls status endpoints for video rendering progress.

---

## 2. Duration Configuration

- `15s` runs in normal Dola chat with the composer switched to Pro/Seedance 2.5;
  duration and ratio are carried in the prompt. Do not enter Create Videos or load the
  debugger extension for this path; it should use Dola's current live skill definitions.
- `10s` and `30s` run through Create Videos. Model, duration, and ratio are selected
  and verified on the action bar before the prompt is submitted.
- The Chromium extension exposes the extended `30s` action-bar option through response interception.

### Account scheduling and concurrency

- Account profiles are reserved atomically; at most one Chromium worker may use a profile.
- Untargeted work is ordered by daily usage and then `last_used_at`, which spreads quota
  evenly instead of repeatedly preferring the alphabetically first account.
- Tasks waiting for a busy profile use an `asyncio.Condition` notification. Releasing a
  profile wakes queued tasks immediately; there is no fixed five-second scheduler poll.
- The global semaphore limits active browser work while per-account reservations prevent
  persistent-profile lock collisions.

---

## 3. High-Definition Stream Processing

- Video rendering status is monitored through the event stream.
- High-definition stream URLs are parsed and downloaded directly to the local storage directory `downloads/`.

---

## 4. Verification Handling

- Handles verification challenges using automated visual template alignment to ensure reliable background execution.
