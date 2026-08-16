# Snakes of Honour — Wipe analyser

Progression-night feedback for a 25-player Mists of Pandaria Classic raid.

It answers one question in the gap between pulls: **why did that pull fail, and
what is the single highest-value thing to change before the next one?**

It is not a DPS meter (Details! exists) and not a rotation checker. It reads
`WoWCombatLog.txt` from disk, locally, and uploads nothing.

---

## Quick start

In Command Prompt or PowerShell. Note the `/d` — plain `cd D:\...` from the C:
drive changes the directory on D: without switching to it, and everything after
it then runs in the wrong place.

```bat
cd /d D:\wipe-verdict
pip install -r requirements.txt
```

**During a raid** — start it before the first pull and leave it on a second
monitor:

```bash
python -m wipeverdict live
```

It finds `WoWCombatLog.txt` on its own, reads what has already been logged
tonight so the pull-over-pull comparison works even if you start it late, then
follows the file. The dashboard is at <http://127.0.0.1:8765>. A verdict appears
a couple of seconds after `ENCOUNTER_END`.

Point it somewhere specific with `--log "C:\...\Logs\WoWCombatLog.txt"`.

**After the fact**, against any saved log:

```bash
python -m wipeverdict pulls  <logfile>                  # what was attempted
python -m wipeverdict report <logfile> --encounter 1606 # full verdicts
python -m wipeverdict report <logfile> --last           # just the last pull
```

`--encounter` matters on a full night: these logs are 400-500 MB and filtering
to one boss avoids parsing the rest.

## Turn on Advanced Combat Logging

**Esc → Options → Network → Advanced Combat Logging**, then `/reload`.

It was ON for 30/07, 09/08 and 11/08 and **OFF on 13/08**, so it can come
unset. Without it the log carries no health values, which costs:

- boss health percentage — the "we got them to 1.1%" number, and with it the
  whole pull-over-pull progression comparison
- health before a killing blow, so the "single melee hit from full health with
  no healing" signature cannot fire

Everything else still works: deaths, inferred killing blows, tank-death cascade
detection, avoidable damage, interrupts, cooldown coverage and roles. The
dashboard shows a warning banner when it detects logging is off, so you find
out at 20:00 rather than after the third wipe.

---

## What it tells you

For each pull: a headline verdict, three to five ranked actions, a comparison
with the best previous attempt that night, the death list with cascade deaths
marked, and the avoidable-damage table.

Ranking is by estimated impact on killing the boss, not by size of the number:

1. What directly caused this wipe
2. Repeated failures across pulls — same mechanic, several players, labelled as
   an assignment problem rather than individual error
3. Deaths before the point the raid usually reaches
4. Unused defensives and raid cooldowns in damage windows
5. Throughput — last, and only when the pull was a damage or healing check
   rather than a survival failure

The list is capped at five. A ranked list of twenty is the same as no list.

---

## How it decides things

Every conclusion states the number behind it, the metric used, and where a
naive alternative exists, why it was rejected. That is deliberate: every
mistake listed below was originally caught by a raider challenging a
conclusion, not by the analysis noticing on its own.

**Cascade deaths are never blamed on anyone.** When a tank dies the boss
reaches whoever is next on threat. Deaths in the following 15 seconds with a
melee killing blow are consequence, not cause, and are excluded from blame. A
death to a *ground effect* in that window is not excused — standing in
something is your own doing even if a tank just died. Tanks are the exception:
a tank dying to melee is the intended target, so it reads as a mitigation gap.

**Killing blows are inferred.** `UNIT_DIED` does not name the killer in this log
format, so it is taken from the last damage to land on the victim. The output
says so rather than presenting a guess as a fact.

**Absorbs are never judged by uptime.** Guard, Power Word: Shield, Blood Shield
and Divine Aegis end when *consumed*, not when they expire, so a tank under
heavy melee shows low uptime however well they play. These are measured by cast
rate against cooldown. The rule is enforced in code — constructing a finding
that applies an uptime metric to a consumed absorb raises `MetricError` — not
left as a comment for someone to re-break later.

**Rates use alive time, not fight duration.** A player dead for 40% of a pull
otherwise shows suppressed everything.

**Ticking mechanics are counted by application, not by tick.** Toxic Mist ticks
708 times across 21 players in one pull. Counted as hits it would outweigh
every real mistake in the report. Set `count: applications` in the config for
anything that ticks.

**Cast count alone is not evidence** when something else refreshes the ability
or no real cooldown governs it. Power Word: Shield is gated by per-target
Weakened Soul, so "casts available = fight length ÷ 6s" is a fiction; the
config sets its cooldown to 0 and the tool declines to claim a target.

**Cooldowns are never suggested as a pair unless they stack.** Balance druid
Celestial Alignment and Incarnation must be sequenced, and that is recorded in
`config/mechanics.yaml` rather than assumed.

**Comparisons never blend difficulties or raid sizes.** A 10-player night is
not evidence about a 25-player one. Nor do they blend mechanics: Thok's breath
depends on which captive he drinks, so the pull-over-pull delta counts only
mechanics that occurred in *both* pulls and names the ones that did not.

**Nobody is told to avoid the unavoidable.** The root cause checks the mechanic
config before phrasing its advice, so a death to raid-wide damage reads as a
healing or cooldown problem rather than "stop standing in it".

**Soaking is the job, not a mistake.** A mechanic someone must stand in is
never counted against them; the failure spell — the one that punishes the whole
raid when a soak is missed — is what gets reported.

**Soak and share counts are measured against the LIVING raid.** Late in a wipe
those counts collapse because most of the raid is dead, while per-player damage
rises for unrelated reasons. Two numbers moving together is not one causing the
other, so casts with fewer than 15 players up are ignored.

---

## Configuration

Mechanics live in YAML so a raider who knows the fight can correct the tool
without touching code:

- `config/bosses.yaml` — per boss. Seeded for all eight bosses from **Kor'kron
  Dark Shaman to Garrosh Hellscream**:
  - `avoidable` — damage nobody should be taking. `count: applications` for
    anything that ticks; `exempt_roles: [tank]` when the role holding the boss
    cannot avoid it; `amount_at_least` when one spell id carries both an
    avoidable and an unavoidable component.
  - `soaks` — something a player must deliberately stand in. Soaking is
    credited, never blamed; `fail_spell_id` is the raid-wide punishment when a
    soak is missed, and that is what gets counted.
  - `shared_damage` — unavoidable but split between everyone hit, so the
    actionable number is how many soaked it.
  - `tank_busters`, `interruptible`.
- `config/mechanics.yaml` — raid cooldowns, consumed absorbs, interrupt
  abilities, non-stacking pairs, reprieve talents.

**Every spell ID was read out of this guild's own logs**, never from a spell
database, so they match this client build. Seed a new boss with:

```bash
python tools/mine_spells.py <logfile> <encounter_id>
```

It prints candidate avoidable mechanics, tank busters and interruptible casts
ranked by how much damage they did and how many players they hit.

Entries marked `verified: false` are guesses from cast bars in the log — the
tool labels findings derived from them so they are not asserted as fact.

If a mechanic hits nearly the whole raid every pull, the tool says so in a
separate "notes on the config" section rather than telling 24 people to move.
That usually means the mechanic is unavoidable and the config is wrong.

---

## Validation status

Verified against this guild's own logs:

- **Kor'kron Dark Shaman, 25 Heroic, 30/07** — 3 pulls (one 1-second reset, one
  wipe at 23.2%, one kill), tanks and healers identified correctly, both bosses
  distinguished from their wolves.
- **Full raid night, 09/08** — 33 encounter boundaries across six bosses,
  including a 10-pull Spoils of Pandaria progression block.
- **Live path** — 204,228 lines replayed into a file in bursts with deliberate
  mid-line splits; all pulls segmented and analysed with no corruption.

**Checked against Warcraft Logs report `3vQrn9Xt4jhDJ8aH` (13/08) — matches.**

| | report | parser |
|---|---|---|
| pulls | 17 | 17 |
| kills | 6 | 6 |
| Dark Shaman wipes | 6 | 6 |

The raw log contains 19 `ENCOUNTER_START`/`END` pairs; the two extra are a 0:14
Iron Juggernaut and a 0:03 Dark Shaman, both resets. Warcraft Logs drops those
too, and `report` skips anything under `--min-duration` (default 30s).

The report's "best 1.1%" could not be checked: **Advanced Combat Logging was
off that night**, so the log carries no health values at all. See below.

---

## Known limits

- **Range is invisible.** The log does not record whether a player was in range
  of an interruptible cast, so "who could have interrupted" means alive and off
  cooldown only. Findings say this rather than implying certainty.
- **Damage windows are derived, not scripted.** They come from the pull's own
  raid-damage peaks, so a new boss works with no timers configured, but a
  window that always hurts will not be distinguished from one that hurt because
  a cooldown was missing.
- **Spec detection is role-level.** Tank/healer/dps is derived per pull from
  output and boss melee taken. Finer spec detection is not implemented.
- **Cascade uses a fixed 15-second window** and a rule that the raid has
  collapsed once 40% of it is dead. Both are constants in `deaths.py`.
- **Boss percentage is meaningless on Spoils of Pandaria.** That encounter has
  no single health bar, so the "closest we got" figure reports whichever big
  unit was last alive. Everything else works there; only that number should be
  ignored.
- **Only Dark Shaman and Blackfuse are configured.** Other bosses still get
  pull segmentation, deaths, cascade classification and cooldown coverage — the
  avoidable-damage table and interrupt findings need a config entry, which
  `tools/mine_spells.py` generates in a minute.

---

## Development

```bash
python -m unittest discover -s tests -v     # 35 tests
python tools/probe_layout.py <logfile>      # re-derive the field layout
python tools/replay.py <src> <target>       # simulate the game writing a log
```

`docs/log_format.md` records what the log format actually is on this build and
how it was established.
