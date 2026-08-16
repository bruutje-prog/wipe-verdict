# MoP Classic combat log format, as it actually is on this build

Established by reading this guild's own logs with `tools/probe_layout.py`, not
from documentation. Re-run that tool after a patch; if a column moves, this
file and the constants in `wipeanalyser/logparse.py` are what need updating.

```
8/11/2026 19:56:46.6141  COMBAT_LOG_VERSION,9,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,5.5.4,PROJECT_ID,19
```

- **Version 9**, **advanced logging on**, build **5.5.4**, PROJECT_ID **19**.
- Timestamp and event are separated by **two spaces**. The sub-second part has
  four digits.
- Quoted fields may contain commas, so a plain `split(",")` shifts columns on
  those lines.

## Encounter boundaries

```
ENCOUNTER_START,1606,"Kor'kron Dark Shaman",6,25,1136,19
ENCOUNTER_END,1606,"Kor'kron Dark Shaman",6,25,0
```

`id, name, difficultyID, groupSize, instanceID, ...` and for END a trailing
success flag. Difficulty **3** = 10 Normal, **4** = 25 Normal, **5** = 10
Heroic, **6** = 25 Heroic.

Encounter IDs seen here: Immerseus 1602, Fallen Protectors 1598, Norushen 1624,
Sha of Pride 1604, Galakras 1622, Iron Juggernaut 1600, Kor'kron Dark Shaman
**1606**, General Nazgrim 1603, Malkorok 1595, Spoils of Pandaria 1594, Thok
1599, Siegecrafter Blackfuse **1601**, Paragons 1593, Garrosh 1623.

## The general shape

```
prefix (fixed, from the START) | advanced block (VARIABLE) | payload (fixed, from the END)
```

The parser indexes the prefix forwards and the payload **backwards**. Blizzard
adds fields to the advanced block between patches; indexing the payload from
the end means such a patch cannot silently shift the damage amount into the
"resisted" column.

- prefix: 8 fields — src GUID/name/flags/raidFlags, dest GUID/name/flags/raidFlags
- spell prefix: 3 more — spellId, spellName, spellSchool. **Absent on
  `SWING_*`.**
- advanced block: **18 fields on this build** — infoGUID, ownerGUID, currentHP,
  maxHP, attackPower, spellPower, armor, …, powerType, currentPower, maxPower,
  powerCost, posX, posY, uiMapID, facing, level-or-item-level
- damage payload: 10 fields — amount, base, overkill, school, resisted,
  blocked, absorbed, critical, glancing, crushing
- heal payload: 5 fields — amount, base, overhealing, absorbed, critical
- `SPELL_*` damage carries a trailing **`ST`/`AOE`** tag. `SWING_DAMAGE` does
  not.

Observed widths: `SPELL_DAMAGE` 41, `SPELL_PERIODIC_DAMAGE` 41, `RANGE_DAMAGE`
41, `SWING_DAMAGE` 37, `SPELL_HEAL` 35, `SPELL_CAST_SUCCESS` 30, `UNIT_DIED` 10.

### Which column is the damage

Proven rather than assumed: on a full-health target the advanced block's
`maxHP - currentHP` equals column 29 exactly, so **column 29 is the applied
amount** and column 30 is the unmitigated base. On a glancing melee blow the
two differ in the direction mitigation predicts, which confirms it a second
way.

## Traps this format sets

**Players log health as a PERCENTAGE, creatures in absolute points.** A raider
reads `88/100`; Earthbreaker Haromm reads `1962590350/1962616500`. Only the
*ratio* means the same thing for both, so only `hp_fraction` is exposed.

**The advanced "info" unit is sometimes the source and sometimes the
destination.** Compare `info_guid` against src/dest before trusting it.

**`COMBATANT_INFO` leads with a player GUID followed by FACTION, not a name.**
It looks exactly like a normal prefix and will register every raider under the
name `"0"`. It is excluded from prefix handling.

**`SWING_DAMAGE_LANDED` duplicates `SWING_DAMAGE`.** Counting both doubles
every melee hit — the same class of error as counting a channelled spell's
ticks as casts.

**Pets are Creature/Vehicle GUIDs on the raid's side.** `not is_player()` is
not the same as "enemy": Mirror Image, Water Elemental and warlock pets all
cast with cast bars, and treating them as enemies fills the interruptible list
with the raid's own pets. Use the reaction bit instead — `flags & 0x40` is
hostile, `0x10` friendly.

**`SPELL_ABSORBED` comes in two widths**, 18 and 21 fields, depending on
whether the attacking spell's prefix is present. The absorber block sits a
fixed distance from the *end*, so indexing backwards parses both.

**Absorbs are not `SPELL_HEAL`.** Discarding `SPELL_ABSORBED` makes a
discipline priest look like a dps who occasionally casts Penance — in one real
pull, 31 of 42 million of Lepotedetpwz's output was absorb.

**`UNIT_DIED` does not carry the killing blow.** It must be inferred from the
last damage event landing on the victim before it.

**Ground effects log `nil` as the source name.** That is not a name and must
not be printed as one.
