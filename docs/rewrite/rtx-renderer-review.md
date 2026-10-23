---
tags:
  - rewrite
  - rendering
  - review
---

# RTX renderer review

Date: 2026-10-23  
Scope: Python RTX rendering, artistic direction, effect interactions, and implementation architecture.

## Executive assessment

This is a strong and unusually thoughtful first version. It already goes well beyond
"bloom plus dynamic lights": the persistent ground materials, analytic beam sources,
creature occlusion, archived explosion visuals, pressure fronts, and simulated smoke
form a real rendering system. The debug views and pixel tests are also excellent
foundations.

The main weakness is not a lack of effects. It is that several individually good
look-development studies still behave like separate layers. Ground, creatures,
players, bonuses, and smoke share a light list, but not a complete lighting model.
They use different falloffs, beam placement, highlight limiters, and occlusion rules.
As a result, one gameplay event can feel like a different physical event on each
surface.

My recommended direction is:

1. **Preserve the original authored art, especially albedo and silhouettes.**
2. **Make Classic-with-no-dynamic-light the baseline, then add RTX energy.**
3. **Use one linear-light source model across every receiving pass.**
4. **Spend new work on cross-effect interactions, not more isolated glow.**
5. **Keep gameplay readability ahead of physical purity.**

The highest-priority visual issue is blood. In matched Survival captures, RTX blood
is flatter, less detailed, and more magenta than Classic's deep red, richly textured
pools. That comes directly from removing the authored blood albedo and reconstructing
it procedurally. I would fix that before adding more effects.

## Validation performed

- Ran the complete display- and asset-backed render suite: **1014 passed, 10 skipped**.
- Reviewed matched Classic/RTX Survival frames at ticks **4395, 5115, and 6255**, at
  960x540, using the same replay and world state.
- Reviewed the ground, material history, light collection, occlusion, character,
  bonus, smoke, explosion, shockwave, beam, HDR, and presentation paths.
- The captures exercised SDR output under Linux/llvmpipe. They were not a validation
  of macOS EDR, Wayland HDR, native-client parity, or every weapon combination.

No broad correctness failure emerged from the suite or static review. The most
important findings are visual coherence and architecture rather than crash fixes.

## What is already very good

### Persistent materials actually interact

The ordered ground history is the best part of the renderer. Blood is not merely a
red decal: it has coverage, volume, wetness, freshness, absorption, relief, and
reflection responses. Later deposits affect earlier ones:

- corpses and casings displace or cover blood instead of simply drawing over it;
- ice attenuates the blood film beneath it;
- blood-over-ice and ice-over-blood remain ordered;
- scorches and later deposits can dry or replace earlier material coverage;
- clean substrate is retained separately, avoiding a second unintended blood tint.

Relevant code: `src/crimson/render/rtx/materials.py:72`,
`src/crimson/render/rtx/materials.py:204`,
`src/crimson/render/rtx/shaders/ground_stamp.frag:20`, and
`src/crimson/render/rtx/shaders/ground.frag:48`.

This is exactly the kind of interaction worth expanding.

### Explosions are coherent world events

A retained explosion visual drives archived fireballs, a warm shell, dynamic light,
and a pressure front. Smoke emissions and atmosphere impulses come from the same
simulation-side effect activity. The shockwave distorts the world and smoke while
the HUD remains stable and readable.

Relevant code: `src/crimson/effects.py:752`,
`src/crimson/render/world/effects.py:147`,
`src/crimson/render/rtx/material_lights.py:370`,
`src/crimson/render/rtx/shockwaves.py:30`, and
`src/crimson/sim/smoke_events.py:35`.

This is a good model for other effects: one event, several coordinated responses.

### Smoke is more than a particle replacement

The smoke has density, temperature, velocity, retention, advection, self-extinction,
height integration, dynamic-light scattering, and laser-sight attenuation. Blasts,
muzzle events, and wakes can move existing smoke without depositing new smoke.
The reduced-resolution composite is a sensible performance boundary.

Relevant code: `src/crimson/render/rtx/smoke_shaders.py:71` and
`src/crimson/render/rtx/smoke_composite.py:26`.

### The engineering support is strong

- Analytic beam bodies avoid large repeated-stamp draw costs.
- Creature occlusion uses bounded spatial data rather than testing every body for
  every possible fragment.
- Independent illumination and reflection budgets recognize that those responses
  do not always need the same sources.
- Late Classic-to-RTX activation is considered explicitly.
- Shared GLSL between Python and the native client reduces shader drift.
- Debug views expose normals, wetness, material coverage, direct light, reflections,
  visibility, smoke density, heat, flow, and scattering.
- Shader pixel tests cover behavior that ordinary state-level tests would miss.

These are all worth keeping.

## Priority findings

### 1. Preserve authored blood albedo instead of replacing it

**Priority: highest visual impact.**

The RTX substrate bake explicitly removes blood decals in
`src/crimson/render/rtx/materials.py:295`. The ground shader then synthesizes blood
from desaturated substrate luminance, absorption, and a nearly constant body color
in `src/crimson/render/rtx/shaders/ground.frag:144`.

That discards too much of the original art. Coverage silhouettes survive, but the
authored chroma, internal contrast, and much of the painted variation do not. The
matched captures show the result: RTX pools become broad maroon/magenta regions,
while Classic pools retain deep reds and much stronger internal detail.

**What I would do differently:**

- Keep a pigment/albedo representation of the original blood artwork.
- Derive height, roughness, wetness, and volume from the companion maps, but use
  those maps to modulate the authored color rather than replace it.
- A simple first step is to retain blood in the albedo target and apply absorption
  as a calibrated multiplier. A better long-term step is a separate low-frequency
  pigment target written by the same ordered stamp operation.
- Use freshness to alter gloss, darkening, and edge meniscus more than hue. Let old
  blood become darker and less saturated without shifting the whole pool toward
  purple.
- Add a no-dynamic-light comparison test. RTX should have a deliberate, documented
  reason for every baseline pixel that differs from Classic.

The same principle should guide future RTX work: **procedural data should enhance
the original pixels, not erase them.**

### 2. Make the HDR pipeline genuinely linear end to end

**Priority: high architectural impact.**

Several lit passes decode albedo, add light, and then gamma-encode the result before
writing the HDR target. Examples include:

- ground: `src/crimson/render/rtx/shaders/ground.frag:347`;
- creatures: `src/crimson/render/rtx/shaders/creature.frag:147`;
- players: `src/crimson/render/rtx/shaders/player.frag:103`;
- bonuses: `src/crimson/render/rtx/shaders/bonus.frag:136`.

The display shoulder then treats those values as its working radiance in
`src/crimson/render/rtx/shaders/hdr_tonemap.frag:24`.

This is a coherent legacy approximation, and the comments are honest about it, but
it is not a unified linear-radiance pipeline. Additive sprite contributions,
premultiplied smoke, lit materials, reflections, local limiters, and the final
shoulder do not all share the same energy meaning. That makes cross-effect tuning
harder and makes SDR-to-EDR behavior less predictable.

**What I would do differently:**

1. Decode source color textures once at sampling.
2. Keep world color, emissive contributions, smoke radiance, and blending linear.
3. Apply exposure and the display shoulder once, at presentation.
4. Encode for the actual display surface once, after tone mapping.
5. Keep raw-data debug views explicitly outside that display transform.

Do this before extensive new art tuning; otherwise the same look will need to be
tuned twice.

### 3. Share light evaluation, not only `MaterialLight` records

**Priority: high interaction impact.**

The common source list is good, but each receiving pass still reinterprets it:

- ground point attenuation uses `(1 - d/r)^2` in
  `src/crimson/render/rtx/shaders/ground.frag:243`;
- character and bonus responses use squared smoothstep falloffs;
- ground and creatures use an energy-centroid approximation for beam illumination,
  while bonuses and smoke use nearest-segment positions;
- ground has specialized continuous beam sections that the other receivers do not
  share;
- creatures, players, and bonus shells use different local highlight limiters;
- visible beam intensity and illumination intensity are authored in separate places.

These differences can be useful material choices, but the source geometry, distance,
visibility, and emitted energy should not change identity from pass to pass.

**What I would do differently:**

Create one shared GLSL source-evaluation library for point, segment, and annulus
sources. It should define:

- source position and extent;
- attenuation and radiance;
- tail/head energy;
- occlusion and finite-source softness;
- optional per-response gains.

Each material should only decide how that incoming radiance interacts with diffuse,
specular, transmission, or scattering. Keep the specialized continuous ground path
as an optimized implementation of the same source definition, with equivalence
tests against the shared approximation.

### 4. Remove negative light and competing local highlight compression

**Priority: high art-direction impact.**

Ion and fire beams reduce ambient ground light through `ION_AMBIENT_DIP` and
`FIRE_AMBIENT_DIP` in `src/crimson/render/rtx/continuous_beam.py:19`, applied in
`src/crimson/render/rtx/shaders/ground.frag:160`.

This can improve contrast, but conceptually it is a negative light. It makes the
area around an energetic emitter darker before adding its illumination, which can
look like a moving shadow or a color artifact rather than emitted energy.

At the same time, creatures, players, and bonuses independently compress added
light before the final display shoulder. Dense fights therefore saturate according
to different rules on different materials, flattening the highlight hierarchy.

**What I would do differently:**

- Establish a darker but readable unlit baseline instead of subtracting ambient
  around selected weapons.
- Let emitters add energy; use a shared exposure/tone-mapping policy to control the
  result.
- Remove or greatly reduce per-material aggregate limiters after linearization.
- If a safety limiter remains necessary for performance or extreme scenes, apply a
  common policy at the light-selection or frame level, not unrelated thresholds on
  each material.

### 5. Make occlusion and compositing consistent across effects

**Priority: high interaction impact.**

Creature bodies participate in the dynamic occlusion grid used by ground and
creature lighting. Players and bonuses receive lighting but do not participate in
that same occlusion system. Bonus and player shaders do not use the shared body
visibility function.

Smoke has good self-shadowing, but its light rays do not include creature bodies.
It is also composited after players, creatures, projectiles, and bonuses in
`src/crimson/render/world/draw.py:112`, so the whole volume effectively sits in
front of every world object.

Finally, native creature blob shadows remain in the normal sprite pass while RTX
adds dynamic body occlusion. Without careful tuning, the static shadow can remain
equally dark inside a brightly lit region, producing a pasted-on double-shadow look.

**What I would do differently:**

- Add players and large bonus shells to the same simple body/occlusion model.
- Use the same body visibility for smoke lighting where it is visually important.
- Split smoke into low/behind and high/in-front contributions, or use a very small
  set of height bands. A full depth-based volumetric renderer is unnecessary.
- Keep only a restrained contact-shadow component from the native blob in RTX;
  let dynamic lights affect the broader shadow.
- Add contact shadows for bonuses before attempting expensive bonus reflections.

These changes would make effects feel embodied in the same world rather than drawn
in the correct order but occupying different physics.

### 6. Stabilize light selection in dense scenes

**Priority: medium-high, especially for late-game combat.**

`select_material_lights` ranks sources by brightness, gain, and distance to the view
center, then keeps independent top-N sets in
`src/crimson/render/rtx/material_lights.py:243`.

This is bounded and sensible, but it has no temporal stability or semantic priority.
During camera movement or rapid intensity changes, a source can cross the cutoff
and abruptly lose ground, character, reflection, or smoke response. Ion arcs can
consume many slots, and the globally selected set is reduced again for smoke.

**What I would do differently:**

- Reserve small budgets for semantically important sources: primary explosion,
  player muzzle, dangerous projectile, then ordinary flames/tracers.
- Add hysteresis or a short fade when a source enters or leaves a budget.
- Rank by contribution to the visible receiver region, not only the screen center.
- Keep deterministic tie-breaking for replay capture and backend parity.
- Test more than 32 simultaneous sources while panning the camera.

The goal is not to keep every light. It is to ensure that budget changes do not
look like effects turning on and off for unrelated reasons.

### 7. Reduce duplicate material/history work and make activation policy explicit

**Priority: medium engineering impact.**

Albedo and material data are produced through related but separate bake paths.
Adding a new terrain effect requires keeping its visual stamp, material kind,
coverage, height, and substrate behavior synchronized. Multiple render targets and
the separate pristine substrate also create a substantial memory cost.

`MaterialHistory` bounds RAM, not total spilled history. A long Classic session can
create a large backlog and a synchronous first RTX bake. The documentation already
acknowledges this, so it is not a hidden defect; it is a product tradeoff worth
revisiting before RTX becomes a default mode.

**What I would do differently:**

- Give each terrain stamp one descriptor containing its Classic albedo operation
  and RTX material outputs.
- Consider multiple render targets for the common stamp path so ordered albedo,
  pigment, coverage, and height writes cannot drift apart.
- Pack companion channels before adding more maps; remove depth attachments where
  they are unused.
- Offer an approximate fast-activation path from the current ground plus recent
  history, with exact replay as an optional debug path. This is preferable to an
  unbounded disk backlog and an unpredictable multi-second hitch.

## Artistic direction

### Keep Crimsonland gritty, not glossy by default

The original art has strong silhouettes, rough terrain, dark blood, and small but
legible effects. RTX works best when it gives those shapes depth and cause-and-effect.
It works worst when every surface becomes a smooth procedural material.

The creature dome normal, painted-luminance relief, thin-part transmission, and
bonus clear coat are useful tools. They should not all have equal prominence on
every object. I would add small per-species or per-material profiles:

- chitin: tighter, directional highlights and harder rim response;
- flesh: broader highlights and restrained thin-part transmission;
- scales: broken, higher-frequency specular rather than a smooth dome;
- cloth/player: mostly diffuse with a weak rim;
- brass: sharp warm glints, but tiny total screen area;
- ice: edge Fresnel and thickness tint, not simply maximum smoothness.

This can remain data-driven and approximate. Hand-authored normal maps are not
necessary for a first pass.

### Protect the baseline and the gameplay hierarchy

The matched frames show that RTX darkens or flattens parts of the scene even when
little dynamic lighting is active. I would adopt this rule:

> With no nearby emitter, RTX should preserve Classic's readability and authored
> color unless a specific environment treatment is intentionally enabled.

From there, establish a strict effect hierarchy:

1. player, incoming threats, and aiming information;
2. dangerous projectiles and explosion timing;
3. creatures and bonuses;
4. blood, terrain, smoke, and persistent material detail.

Wet reflections should support weapons and explosions, not turn old blood into a
mirror that competes with live threats. Smoke may obscure the world briefly, but it
should not erase the player or critical warning cues for long periods.

### Use emissive-only bloom, if bloom is added

The current HDR path has a display shoulder but no unified emissive buffer. On SDR,
that can make RTX look like darker Classic plus local relighting rather than a clear
improvement.

After linearization and baseline correction, a restrained half-resolution bloom can
help. It should be driven by tagged emissive sources, not a whole-frame luminance
threshold. White corpses, bright terrain, and the HUD should not bloom. Keep the
HUD after tone mapping and bloom as it is now.

### Avoid effects that would fight the game

I would not add generalized chromatic aberration, strong film grain, constant camera
distortion, indiscriminate bloom, or a light for every particle. I also would not
turn Monster Vision into an ordinary physical lamp; it is primarily a gameplay
overlay. Sharpshooter's restrained reflection/scattering treatment is a good example
of an effect that should not become broad red illumination.

## Effect interaction inventory

| Effect or material | Strong interactions already present | Missing or worth changing |
| --- | --- | --- |
| Muzzle flashes and projectiles | Shared illumination of ground, creatures, players, bonuses, and smoke; wet-ground reflections; restrained tracers | Common falloff/source geometry; player and bonus occlusion; stable budgets |
| Ion and fire beams | Analytic visible body; segment lighting and reflections; continuous ground response; smoke scattering; ion arc sources | Remove negative ambient; match visible and received energy; share continuous source model; add ice/material-specific response |
| Explosions | Fireball, warm shell, light, pressure refraction, smoke, blast airflow, and scorch are coordinated | Hot-smoke emission; body occlusion in smoke; heat response on ice and wet ground |
| Blood, corpses, brass, and scorch | Ordered coverage, displacement, wetness, aging, absorption, relief, and reflections | Preserve authored albedo; add subtle movement disturbance/footprints later |
| Ice and freeze | Ground ice attenuates underlying blood and changes roughness/height; ordered blood/ice composition | Lit frozen-creature material; edge refraction; heat shatter/melt and steam; avoid a flat unlit overlay |
| Creatures and players | Creatures cast body-proxy shadows and receive diffuse/specular/transmission; players receive cloth lighting | Players as occluders; per-species materials; restrained contact shadows; optional simplified wet-ground silhouettes |
| Smoke | Density, heat, advection, self-extinction, dynamic scattering, sight attenuation, and blast response | Smoke-to-world shadowing; body occlusion; front/behind entity composition; source-dependent soot/heat/steam |
| Bonuses | Obsidian shell, foil emblem, clear-coat response, dynamic reflections and caustic suggestion | Contact shadow; shared visibility; less isolated studio-light character |
| Shockwaves | Distort world and smoke together, preserve HUD, reuse explosion shell timing | Optional localized wet-surface ripple; avoid stronger full-screen distortion |
| Radioactive, Monster Vision, Sharpshooter | Distinct responses rather than one generic light; sight gets narrow scattering/reflection | Preserve their gameplay-specific roles; do not make every perk physically equivalent |

## New interactions I would add

These should be presentation-only and must not consume gameplay RNG or alter
simulation outcomes.

### 1. Heat, soot, and steam

Use the existing smoke temperature field more meaningfully:

- fresh explosion smoke gets a short warm emissive contribution that cools quickly;
- ordinary old smoke remains neutral and non-emissive;
- hot effects crossing ground ice create a brief white steam plume and a fading melt
  or crack response;
- scorches can temporarily suppress wet reflections before the surface cools.

Currently every emission adds temperature in
`src/crimson/render/rtx/smoke_simulation.py:154`, while heat mainly changes the
column shape rather than providing source-dependent radiance. Separate soot,
temperature, and steam source profiles instead of treating all smoke as one gray
medium.

### 2. Frozen creatures as a real material state

`draw_freeze_overlay` currently draws a translucent sprite over creatures in
`src/crimson/render/world/draw.py:443`. Feed freeze into the creature material instead:

- reduce or recolor transmission;
- add thin edge Fresnel and restrained specular;
- preserve the underlying silhouette and lighting;
- trigger a brief shatter glint and cold steam at the appropriate presentation event.

This would connect the freeze effect to the same light system as everything else.

### 3. Wet-ground disturbance

After the core material look is fixed, add subtle, short-lived disturbances:

- player/creature movement through fresh deep blood creates a fading wake or a few
  dark footprints;
- heavy explosions send a small ripple through nearby pool normals;
- casings landing in a pool produce a tiny local glint or meniscus disturbance.

Keep these low contrast and short lived. They should reward close inspection, not
turn every fight into noisy water simulation.

### 4. Grounded entities and selective reflections

Add players and bonuses to contact occlusion first. Then consider very cheap,
low-resolution silhouette reflections for large entities on only the smoothest,
freshest pools. Do not attempt full scene reflections; the current analytic emitter
reflections already cover the most valuable cause-and-effect.

## Architecture I would aim for

I would keep the forward 2D renderer and analytic approximations. A general deferred
renderer or full volumetric system would add complexity without solving the most
important problems.

### Shared presentation event

Each visual source should be able to publish a render-only descriptor containing:

- visible geometry and lifetime;
- linear emissive color and intensity;
- point/segment/annulus representation;
- height and source width;
- illumination, reflection, smoke, and priority responses;
- optional heat, soot, steam, and pressure outputs.

The visible draw and the lighting response should sample the same age, alpha, and
geometry. The existing explosion visual is already close to this model and is a good
reference.

### Shared receiver library

Use one source-evaluation implementation across ground, creatures, players,
bonuses, and smoke. Keep material BRDFs separate. Share body occlusion where the
receiver needs it, and keep optimized approximations behind equivalence tests.

### Simple frame graph

1. Update persistent terrain albedo/pigment/material targets in stamp order.
2. Collect visible presentation sources with stable budgets.
3. Build shared body occlusion.
4. Draw world receivers into linear HDR color plus a tagged emissive target.
5. Composite smoke in explicit low/behind and high/in-front bands.
6. Apply pressure fronts and restrained emissive bloom.
7. Tone map and encode once for the output surface.
8. Draw HUD and gameplay overlays without bloom or world distortion.

This is a refinement of the current architecture, not a rewrite.

## Recommended sequence

### Phase 0: lock down comparisons

- Add RTX options and matched Classic/RTX capture support to replay rendering.
- Create a small set of golden scenes: no-light blood, fresh/old blood, ice over
  blood, dense ion fire, explosion in smoke, frozen creatures, and more than 32
  competing lights.
- Record GPU time and memory per pass on representative hardware.

`ReplayPlaybackMode` already accepts `rtx=True`, but the video-renderer construction
in `src/crimson/replay/driver/replay_render.py:210` does not expose it through the
CLI. Adding `--rtx` and `--no-rtx-smoke` would make future art reviews much easier.

### Phase 1: unify energy and fix the baseline

- Move world rendering to linear color and one display transform.
- Share source geometry and attenuation.
- Remove negative ambient and reconcile local highlight limiters.
- Restore authored blood albedo and establish no-light Classic/RTX comparisons.

### Phase 2: connect the existing passes

- Add player/bonus occlusion and restrained contact shadows.
- Introduce smoke height bands and body visibility where needed.
- Stabilize light selection with semantic budgets and transition handling.

### Phase 3: add high-value interactions

- Source-dependent hot smoke, soot, and steam.
- Freeze as a lit creature material.
- Ice/heat and wet-ground/scorch responses.
- Per-species creature material profiles.

### Phase 4: polish and optimize

- Tagged emissive bloom.
- Wet footprints and subtle pressure ripples.
- Selective large-entity reflections if they remain readable and affordable.
- Packed material targets, MRT stamp output, and an explicit late-activation policy.

## Bottom line

The renderer already has the difficult ingredient: several systems genuinely know
about one another. I would not replace it with a conventional bloom pipeline, and I
would not focus on adding more independent effects.

I would first restore the original blood artwork, unify linear lighting and source
evaluation, and make occlusion/composition consistent. Then I would invest in hot
smoke/steam, lit freezing, grounded entities, and small wet-ground disturbances.
That path preserves Crimsonland's identity while making RTX feel like a coherent
world simulation rather than a collection of shader upgrades.
