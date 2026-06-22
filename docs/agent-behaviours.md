# Agent behaviours — what the real-time data agent does

> The living spec of **what the bot actually does**. Each behaviour is a rule the agent follows.
> PH principle: the agent **converges on facts from real sources, shown transparently** — it never
> writes content. See [[issue-ai-realtime-crawler]] for the full vision.

## Behaviour: aggregator as DISCOVERY, source as TRUTH
**Rule:** Aggregators (ski.com.au, OnTheSnow, etc.) are used to **discover** what data/cams exist and
to **detect changes** — NOT as the data source we depend on. Wherever possible, the agent **goes to
the original source** and captures that directly.

For each aggregator (e.g. **ski.com.au**):
1. **Check hourly for changes** — re-scan the aggregator's cam/report index. Detect: new cams added,
   cams removed, URLs changed, a resort newly listed.
2. **Resolve to the true source** — for each item the aggregator lists, find + record the ORIGINAL
   source (the resort's own cam feed, the Roundshot/operator URL behind the aggregator's mirror).
   Capture from THAT, not the aggregator's re-hosted copy.
3. **Bypass the aggregator for the actual grab** — once the true source is known, grab frames/readings
   from the source directly. The aggregator is only re-checked hourly for *changes*.
4. **Fallback** — if the true source is unreachable/unknown, fall back to the aggregator's copy (and
   flag it as `via:aggregator`, lower trust).

**Why:** not dependent on one aggregator; we get the real frame (no re-compression/staleness from a
mirror); resilient if the aggregator changes or dies; honest provenance (source vs via-aggregator).

### ski.com.au specifically
- It **aggregates the Australian resort cams** (Perisher front-valley/blue-cow/snow-stake, Thredbo,
  Buller, Hotham...) at `cams.ski.com.au/mobile/<cam>.jpg` and a `/snowcams/` index.
- **Use it to discover the AU cam list + detect changes hourly.** Then resolve each to the resort's
  own cam where possible and capture from there. Only use ski.com.au's copy as fallback.
- **Snow-stake cams** are high value — they show a measured depth pole; Ollama vision can read the
  number (a real depth reading, not a model estimate).
- **Finding (2026-06):** ski.com.au **hides the upstream source** — its cam pages mirror the frame
  (`cams.ski.com.au/mobile/<cam>.jpg`) without exposing the resort's original feed URL. So for these,
  source-resolution fails → fall back to ski.com.au's copy, flagged `via:aggregator` (lower trust),
  and keep trying to discover the true source elsewhere (resort site, Roundshot directory).

## Behaviour: self-pruning (remove bad sources immediately)
A source that goes stale / dead / implausibly divergent is **auto-removed** (reason logged) and stops
being shown to users at once. No bad/stale source lingers. (Implemented: webcam.alive +
auto_removed_at/removed_reason; weather flags for divergence.)

## Behaviour: self-extending (add discovered sources, gated)
The agent can ADD a newly-discovered source ("this resort has a temp monitor / a new cam"), but it's
not user-visible until it passes a confidence/liveness threshold.

## Behaviour: transparent multi-source (user validates)
Show ALL sources side by side (source X 45cm · source Z 50cm · cam looks fresh), with provenance +
last-updated, so the USER validates. The agent aggregates + cross-checks; it does not author prose.

## Behaviour: torn-frame guard
Cam frames grabbed mid-write are torn (partial JPEG). The agent validates completeness (JPEG ends
FFD9 / PNG ends IEND); if torn, waits + retries so it never stores a half-updated frame.
(Implemented in scripts/webcam/snowcam.py.)

## Behaviour: link liveness
Every link shown to users (resort sites, reports, cams, booking/operator links) is continuously
verified alive. Dead → flag + (where possible) find the replacement. (Highest-value first phase.)

_Living doc. Add a behaviour here whenever the agent gains a new rule. Implemented behaviours cite
their script; planned ones are flagged._
