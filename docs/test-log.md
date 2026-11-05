# Test log

One row per gate run (`Agents.md` section 11). IDs are from `Architecture.md` section 14.

| Date | Phase | Commit | Machines / OS | IDs run | Result | Bugs opened |
|---|---|---|---|---|---|---|
| 2026-11-05 | 0-5 (automated part) | none yet; the repository has no git history | 1 machine: Fedora 43, Linux 7.2, Python 3.14.8, PyQt6 6.10.2, loopback only, Qt offscreen | 91 IDs from `scripts/gate.py --phase 5`, with `LANVAS_FULL_SOAK=1` | All 91 pass; none fail, are missing, or skip. **Gate NOT GREEN:** 13 items still need other machines or people (below) | none open |

## Honest status

**No phase gate is green yet, and no `phase-N-green` tag is justified.** Each gate also requires things that one Linux machine cannot show. Everything an automated run on this machine can prove has passed.

### Still needed from the team

| Item | Needs | Gate |
|---|---|---|
| `check.py` and XP-06 on Windows/NTFS | a Windows machine | 0 |
| XP-01 | full L2 suite on Windows and a second Linux distro | 5 |
| XP-02, first L3 run, Tier 1 and Tier 2 exit L3 runs | 3+ physical machines, mixed OS, no internet (`Architecture.md` 14.6) | 3, 4, 5 |
| XP-03 on every other demo OS | run `python -m build.make` and the `--smoke` check there | 3, 5 |
| XP-04 | try the firewall commands in `README.md` on real Windows and Linux hosts | 3 |
| PF-04 on real hardware | real GPU and display; the 102 fps here is software-rendered and offscreen | 5 |
| `Design.md` 13.1 manual checklist, 13.3 usability test, 13.4 HiDPI | people and real displays | 4, 5 |
| Tier 1 four-machine LAN test | required by `ProductContext.md` section 6 before any Phase 6 work | 6 |

### Measured on this machine (loopback, so not LAN figures)

| ID | Target | Result |
|---|---|---|
| PF-01 op broadcast, 5 clients | median < 50 ms, p95 < 150 ms | median 1.6 ms, p95 2.0 ms |
| PF-03 load 1000 objects | < 1 s | 9 ms (without painting) |
| PF-04 pan/zoom, 1000 objects | at least 30 fps | 102 fps (software, offscreen) |
| PF-05 server idle CPU, 5 clients | < 5% | 0.3% |
| PF-06 fsync under load | PF-01 still met | median 1.6 ms, p95 2.8 ms |
| CH-01 chaos soak | hashes equal | passed at the full 300 ms, 20% duplicates, 60 s |

Real networks will be slower than loopback. PF-01 has to be measured again in the L3 run.

### How some IDs were tested

- **CH-03** drops connections far more often than the catalogue's 5-15 s, over a shorter run.
- **DS-05** sends one real broadcast between sockets sharing a port. Several real interfaces need the L3 run.
- **XP-03** runs the packaged binaries with no Python environment, but offscreen, and on this OS only.
- **Screens** in `docs/screenshots/` are offscreen renders against a real server process, with other participants played by scripted bots. They are not photos of a multi-machine session. Retake them on real machines for the PPT.

### Not built

Phase 6 (Tier 3 and Signature) was skipped, as `ProductContext.md` section 6 requires until Tier 1 passes a 4-machine LAN test.

Smaller gaps against `Design.md`, none in a gate's automated IDs:

- Launcher rows appear and disappear immediately; there is no slide or fade animation (8.1).
- Dark mode is a `--dark` launch flag, with no switch in the UI (12).
- No ungroup command, and the "Following" people option is Tier 3.
- Snap guides have no distance labels (5; marked stretch in the spec).
- The optional "Editors can approve requests" setting is not built (7.3).
- A viewer's cursor is marked by the words "(viewing)" rather than an eye icon (5).
- There is no separate Pages/Frames section in the left panel (2); frames are rows in Layers.

### Open question for the team

`ProductContext.md` section 12, step 6 says a DM "shows as «private» on C" in the Packet Inspector. In fact C's inspector shows nothing at all, because the server never sends C that frame (CT-02). That is the stronger property, so the code keeps it. The demo script's wording should change. The `«private»` scrubbing exists in `client/transport.py` and is tested, but with server-side routing it has nothing to act on.

## Bugs found and fixed during this run

Each has a regression test, unless the fix is itself in a test.

| Found by | Bug | Severity |
|---|---|---|
| SV-10 | Encrypted files were not recognized: canonical JSON sorts `ciphertext` first, past the marker search | S2 |
| SE-04 | A wrong-type string was reported as `LIMIT_EXCEEDED` rather than `BAD_SCHEMA` | S3 |
| Reading the code | `validate_op` refused `z` and `parent` in update patches, which would have broken every reorder | S2 |
| Review | Joining an unreachable address retried forever with no feedback | S2 |
| Review | Leaving a room while offline deleted the journal holding unsent work (data loss) | S1 |
| Looking at renders | Share badge and request toast stayed stale after approval on another device | S3 |
| Looking at renders | Toasts stacked without limit over the panels | S3 |
| Looking at renders | Lock emoji rendered as nothing without an emoji font; several other glyphs were missing from Inter | S3 |
| Looking at renders | White text on the dark private strip was below 4.5:1 | S4 |

## Latest gate report

```
# Gate report: phases 0..5

## Phase 0
- FR-01: PASS (2)
- FR-02: PASS (1)
- FR-03: PASS (3)
- FR-04: PASS (3)
- FR-05: PASS (2)
- FR-06: PASS (11)
- SV-03: PASS (15)
- XP-06: PENDING (8 automated pass; needs: also needs a run on Windows/NTFS (Linux covered by SV-03))
- manual: PENDING (check.py on Windows)

## Phase 1
- OR-01: PASS (1)
- OR-02: PASS (1)
- OR-03: PASS (1)
- OR-04: PASS (2)
- OR-05: PASS (2)
- OR-06: PASS (2)
- RC-02: PASS (1)
- RC-03: PASS (3)
- RC-05: PASS (3)
- RC-06: PASS (1)
- RC-07: PASS (1)
- CT-01: PASS (3)
- CT-02: PASS (1)
- CT-04: PASS (1)
- CT-06: PASS (2)
- CT-07: PASS (7)
- CT-08: PASS (2)
- CT-09: PASS (1)
- CT-10: PASS (7)
- CT-12: PASS (1)
- SV-01: PASS (3)
- SV-03: PASS (15)
- SV-04: PASS (1)
- SV-05: PASS (2)
- SV-06: PASS (1)
- SE-01: PASS (2)
- SE-02: PASS (2)
- SE-03: PASS (1)
- SE-04: PASS (18)

## Phase 2
- AC-01: PASS (38)
- AC-02: PASS (3)
- AC-03: PASS (2)
- AC-04: PASS (1)
- AC-05: PASS (1)
- AC-06: PASS (4)
- AC-07: PASS (5)
- AC-08: PASS (1)
- AC-09: PASS (3)
- AC-10: PASS (2)
- AC-11: PASS (1)
- AC-12: PASS (2)
- CT-05: PASS (2)
- CT-11: PASS (1)
- SV-02: PASS (1)

## Phase 3
- DS-01: PASS (2)
- DS-02: PASS (2)
- DS-03: PASS (3)
- DS-04: PASS (3)
- DS-05: PASS (3)
- DS-06: PASS (8)
- DS-07: PASS (3)
- DS-08: PASS (2)
- RC-01: PASS (1)
- XP-03: PENDING (1 automated pass; needs: built executables launched on every other demo OS)
- XP-04: PENDING (needs: firewall rules verified on Windows and Linux)
- manual: PENDING (L3 LAN run (first))

## Phase 4
- UN-01: PASS (1)
- UN-02: PASS (2)
- UN-03: PASS (2)
- UN-04: PASS (1)
- RC-04: PASS (1)
- CT-03: PASS (2)
- SV-07: PASS (4)
- SV-08: PASS (1)
- SV-09: PASS (6)
- SV-10: PASS (3)
- EX-01: PASS (7)
- EX-05: PASS (3)
- manual: PENDING (Design.md 13.1 Tier 1 rows)
- manual: PENDING (L3 LAN run on 3+ machines (Tier 1 exit))

## Phase 5
- UN-05: PASS (1)
- SV-11: PASS (2)
- SV-12: PASS (2)
- SV-13: PASS (1)
- EX-02: PASS (1)
- EX-03: PASS (2)
- EX-04: PASS (3)
- EX-05: PASS (3)
- EX-06: PASS (2)
- EX-07: PASS (2)
- EX-08: PASS (1)
- CH-01: PASS (1)
- CH-02: PASS (2)
- CH-03: PASS (1)
- PF-01: PASS (1)
- PF-02: PASS (1)
- PF-03: PASS (1)
- PF-04: PENDING (1 automated pass; needs: frame rate on real GPU and display)
- PF-05: PASS (1)
- PF-06: PASS (1)
- XP-01: PENDING (needs: L2 suite on Windows and a second Linux distro)
- XP-02: PENDING (needs: server and clients on different OSes (L3))
- XP-03: PENDING (1 automated pass; needs: built executables launched on every other demo OS)
- manual: PENDING (full Design.md 13.1)
- manual: PENDING (L3 LAN run (Tier 2 exit))

Gate NOT GREEN: 13 item(s) outstanding
```
