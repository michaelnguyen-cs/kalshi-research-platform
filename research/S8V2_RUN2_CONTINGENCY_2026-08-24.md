# S8-v2 RUN 2 CONTINGENCY

**Pre-committed 2026-08-24, before Run 2's window is selected and before any
Run 2 data is collected.**

Governs `research/THREAD_A_EQUIVALENCE_V2_2026-08-23.md` (frozen at `5a0b4ca`,
section 12 amendment frozen at `a46f39f`). This document adds no parameter and
changes nothing in that one. It fixes in advance what each Run 2 outcome means,
so that no outcome can be reinterpreted after it is seen.

---

## Run 1

Formal Run 1 observed zero decoder-equivalence failures across approximately
2.18 million paired events, but did not pass section 8-v2 because
boundary-evidence sufficiency was not met. Section 8-v2 remains unresolved, not
failed.

---

## Outcome branches

**Branch 1 — Run 2 PASSes:** section 8-v2 passes.

**Branch 2 — Run 2 FAILs:** investigate the FAIL. No redesign converts a FAIL to
insufficiency.

**Branch 3 — Run 2 yields `INSUFFICIENT_BOUNDARY_EVIDENCE` caused exclusively by
`EXCLUDED_GUARD_BAND`, with every true FAIL condition clean and criterion 5's
600,000-event and 60-market floors satisfied:** section 8-v2 is retired as
inadequate for acceptance rather than tuned and rerun a third time. A new
section 8-v3 must be preregistered before collecting any new acceptance data.
The two v2 runs remain immutable engineering evidence and are not reclassified
as PASS. A v3 must reconsider the role of the planned guard band, not adjust 20
to 30 or 1% to 1.5%.

**Branch 4 — Run 2 insufficient for any other reason:** handled under the
existing frozen rules. The guard-band contingency is not invoked.

---

## Refactor rule

The streaming rewrite of `evaluate_archive()` is treated as a substantive
section 8 implementation change unless a later review of the exact diff
establishes otherwise. Run 2 uses the frozen evaluator behaviour associated with
`4c23ec5`. The refactor may be developed separately against the preserved probe
and formal archives as regression fixtures but must not be deployed into the
acceptance path before Run 2.

---

## Drift guard

Recorded here as an operation to be applied **manually on the VM** before Run 2.
`/home/ubuntu/run_s8v2.sh` is outside this repository and is not edited from
here.

```bash
REPO=/home/ubuntu/kalshi-bot
if ! git -C "$REPO" diff --quiet 4c23ec5 --       src/depthrec src/kbot scripts/depthrec_equivalence_v2.py; then
  echo "ABORT: acceptance-path files differ from frozen 4c23ec5"
  git -C "$REPO" diff --stat 4c23ec5 --       src/depthrec src/kbot scripts/depthrec_equivalence_v2.py
  exit 3
fi
if [ -n "$(git -C "$REPO" ls-files --others --exclude-standard --       src/depthrec src/kbot)" ]; then
  echo "ABORT: untracked files in guarded acceptance-path directories"
  git -C "$REPO" ls-files --others --exclude-standard --       src/depthrec src/kbot
  exit 3
fi
cd "$REPO" || exit 3
```

### Import-closure basis for the guarded paths

The guarded set is the transitive import closure of the acceptance path, not a
guess at it. Starting from `scripts/depthrec_equivalence_v2.py`, a static AST
closure and a runtime `sys.modules` sweep agree on **18 repo-local files**. The
guard as first recorded covered **12 of 18**.

The six that were missing, all imported and executed, none of them pinned by any
other mechanism:

| file | why it is in the acceptance path |
|---|---|
| `src/kbot/__init__.py` | package init, loaded on any `kbot.*` import |
| `src/kbot/config.py` | `Settings` / `settings` -- WebSocket and API hosts, credential paths |
| `src/kbot/log_config.py` | `configure_logging`, called by the CLI |
| `src/kbot/kalshi_client.py` | `KalshiClient`, used by the census that builds the universe |
| `src/kbot/auth.py` | reached through `kalshi_client` for request signing |
| `src/kbot/quantity.py` | reached through `kalshi_client` |

`src/kbot/config.py` alone determines the WebSocket host and the universe the
census enumerates, so a change to it would alter what Run 2 records while the
unwidened guard stayed silent. `src/kbot` is therefore added to both the diff
check and the untracked-file check above.

**Boundary of what the guard covers, stated so it is not mistaken for
everything.**

`scripts/market_calibration_study.py` and `scripts/test_longshot_rule.py` are
read and hashed by `integrity.verify_or_raise` rather than imported. They are
already fail-closed twice over -- the integrity check aborts startup on any
change, and its expected digests live in `src/depthrec/pinned_hashes.py`, which
the guard covers -- so they are deliberately not added here.

`.env` and `kalshi_private_key.pem` are read at runtime but are gitignored
non-code. A git-diff guard cannot cover them, and this one does not claim to.

---

## Scheduling

Section 8.4 of the frozen document, quoted verbatim and in full. The fenced
block reproduces the source bytes exactly, including line breaks and emphasis
markers.

```markdown
### 8.4 Scheduling

The formal acceptance run is scheduled during a window containing live MLB games
in the frozen validation universe, chosen **solely from published game schedules
and before any acceptance data from that run are inspected**. The window may not
be selected or changed on the basis of decoder agreement, event counts observed
after the run starts, or any other equivalence output.

If a completed run is INSUFFICIENT **solely** because criterion 5 was not
reached, it may be repeated unchanged during the next pre-declared eligible
live-game window. A FAIL is not rerun.

High-traffic windows are not "easier": more events give a deterministic decoder
more opportunities to disagree. The scheduling rule exists because MLB supplied
257 of the 259 qualifying markets in the calibration probe, so an hour without
live MLB is expected to have substantially less qualifying-market breadth. The
probe does not establish that no other hour could reach 60 through other series;
it establishes where the breadth came from in the window that was measured.
```

### Authority for Run 2

Section 8.4's rerun clause is conditioned on a run being INSUFFICIENT solely
because criterion 5 was not reached. Run 1 was INSUFFICIENT_BOUNDARY_EVIDENCE
with both criterion-5 floors satisfied, so section 8.4 does not literally
authorize Run 2. Authority comes from section 12's clause that such a run "may
be rerun unchanged in the next pre-declared eligible window." Section 12 omits
the words "live-game". For Run 2, we precommit that "eligible window" retains
section 8.4's live-MLB scheduling constraint.

Section 8.4 gives an eligibility predicate and no rule for choosing among the
windows that satisfy it. The rule below supplies that choice. It was committed
before any candidate schedule was inspected.

### Window-selection rule

Each published first-pitch time `c` (America/New_York) of a validation-universe
game defines one candidate window `[c, c + 60 minutes)`. Eligible candidate
windows must start strictly after the commit timestamp of this window-selection
rule. The target date is the earliest calendar date containing at least one such
future candidate window. Within that date, the score of a window is the number
of validation-universe games whose published first pitch falls within it. Select
the window with the highest score; break ties by the earliest `c`.

Inputs are published start times only. Run 1 market counts, boundary-exclusion
counts, equivalence results, actual game durations and observed Kalshi activity
are not inputs. No game-count threshold is introduced; the rule is an argmax
over a schedule-derived score. The only constants are the 60-minute window
already frozen by section 8.0 and "at least one game", the minimum non-
degenerate value section 8.4's wording implies.

**Disclosed limitation.** The score counts only games beginning inside the
window, never games already in progress, because counting those requires a
game-duration assumption that published schedules do not supply. A window in the
middle of a long slate can therefore score low despite many live games. Applied
to the published 2026-08-23 schedule this rule selects 13:35-14:35 ET, not the
14:35-15:35 window Run 1 used.

**Difficulty direction.** A start-anchored window has fewer markets live at
window start than a mid-slate window, and fewer live markets means fewer
opportunities for a guard-band burst, so this rule may be weakly easier than the
alternative. Selecting the alternative on that basis would be equally
outcome-informed. The rule is chosen on measurement validity -- it captures
section 8.4's "window containing live MLB games" using only published
information -- and the net difficulty effect is recorded as undetermined.

The cutoff for "strictly after" is this rule's own commit timestamp, Git's
committer timestamp (`%cI`) of the commit that introduced this section. A
document cannot contain its own commit timestamp, so the Git history is the
record: `git show -s --format='%H %cI' <this commit>`.

---

## Provenance

Written before Run 2's window was selected and before any Run 2 data existed.
Nothing in it may be revised on the basis of a Run 2 result; a branch chosen
after seeing the outcome it governs would not be a pre-commitment.
