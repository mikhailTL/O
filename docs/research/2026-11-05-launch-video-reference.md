# Launch-video reference: "Perplexity Computer" (YouTube qOsEYyQuxbU, 94 s, 1920×1080)

Frame-by-frame notes taken at 1 fps over the whole video and at 8 fps across two transitions
(12.6–13.5 s, 88.2–89.3 s), captured 2026-11-05 from the official Perplexity channel. Observations only;
nothing here is O's claim about Perplexity's product.

## Timeline

| t (s) | what is on screen | motion |
|---|---|---|
| 0–1 | empty warm off-white canvas → eyebrow "Introducing" + wordmark | fade in, hold 1 s |
| 2–4 | ONE control (Ask / Computer segmented toggle) centred, very large; cursor glides in, clicks | camera push-in: control scales up over 3 frames; the clicked segment slides |
| 5–12 | the composer alone on the canvas; the prompt types in character by character; the box grows with the text | typewriter ~8 s for a 40-word prompt; no other UI |
| 13 | canvas clears to one line "Running Subagents" | kinetic type: luminance wave left→right across the letters, then the line shrinks away (~0.5 s) |
| 14–17 | task list appears one line at a time, each with a small model-tag chip; the list scrolls up as lines arrive | stagger ≈ 100 ms per line, slide 8–12 px + fade |
| 18–27 | assistant prose streams with a cursor; a comparison table fades in; paragraphs appear, then fade/blur away | typewriter + crossfade |
| 28–32 | spreadsheet screenshot: small centred card → full bleed | slow push-in (Ken Burns), then crossfade |
| 33–42 | result card + file chip; cursor click; a dark web-app screenshot scales in; "Scheduled tasks" card appears | scale-in from 0.9 with ease-out; cards stack |
| 43–59 | second prompt typed; progress cards (downloading / trimming) with bars; vertical video card; notification card "Ready for Review" slides in | 55 s: gaussian blur-out to the next scene |
| 60–71 | research table rows fill in with stagger; three app screenshots stacked with depth; "Connect Slack" card button → "Connected" | row stagger; state change on the button |
| 72–76 | notification card; "Connect to thousands of apps" with ~25 app logos drifting around it | slow parallax drift, serif headline |
| 77–85 | alternating composition: screenshot stack on one side, serif headline on the other — "Computer researches / builds / codes / schedules / creates" | the verb crossfades on the same baseline while the noun stays |
| 86–87 | "Computer works." centred serif | hold |
| 88–93 | product glyph wobbles (one squash-tilt bounce), morphs into the brand mark, wordmark slides in, blur-out to black | ~5 s outro |

## Grammar worth copying (and what O does differently)

- One idea per frame, huge negative space, one background colour for the whole film. O: one background — black glass; the only light is white.
- Beats of 1–1.5 s; ~25 scenes in 94 s (≈3.7 s per scene). O's current cut averages 10+ s per caption — too slow.
- The product is shown as isolated UI fragments at large scale (a toggle, a composer, a list), not as a full desktop recording with a caption bar. O: isolate the glass composer, the action-log rows, the Runs tree, the orb.
- Prompts are typed live; results stream; lists stagger in; screenshots push in slowly; scenes leave by crossfade or blur, never by hard cut to a caption card.
- Claims are set in a serif display face, short, verb-led ("Computer builds"). O: "O logs. O heals. O replays." — same device, factual verbs only.
- No corner badges, no lower-third caption bars, no "DEMO 1" eyebrows. O keeps the one honesty line ("chat turns are scripted") as a single quiet frame at the start, not a persistent badge.
- Outro: mark animates, wordmark slides in, blur to black.

## Rules for the O cut

- Palette: black glass (`#0b0b0d` sheet at 86 %, blur), white at three opacities (text 96 %, muted 70 %, hairline 8 %), one soft white glow for emphasis. No blue, no orange, no purple wash, no gradients other than the radial glow.
- Type: the panel's own families; serif only for the claim lines; sentence case everywhere.
- Motion: transform/opacity only; ease-out `cubic-bezier(.23,1,.32,1)`; 200–500 ms for UI, 600–900 ms for camera push-ins; stagger 80–120 ms; crossfade 400 ms; blur-out 500 ms.
- Length: 90–120 s launch cut; each long demo trimmed to ≤ 35 s with the same grammar.
- Honesty: everything shown is the real engine/proxy/log; scripted chat turns are stated once; no competitor named, no numbers.
