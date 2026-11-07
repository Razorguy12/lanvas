# Agents.md

> Instructions for AI coding agents (and humans) working on LANvas. Read this first, then `ProductContext.md`, `Architecture.md`, and `Design.md`.

---

## 1. Mission

Build a **Figma-style, LAN-only collaborative whiteboard** in Python with **raw sockets**, **tkinter**, **creator-approved access control**, live board discovery, room and private chat with retained history, secure save with crash recovery, and export to PNG/JPEG/SVG/PDF/`.lanvas`.

Work proceeds **phase by phase** (§11): finish a phase, test it, pass its gate, then start the next. Repeat until the whole project is complete and tested.

## 2. Source of truth (priority order)

1. `Architecture.md`: protocol, data model, persistence, security (do not contradict)
2. `ProductContext.md`: scope and tiers
3. `Design.md`: UI behavior and tokens
4. This file: process, phases, and conventions

If code and docs disagree, **stop and flag it**; do not silently diverge. If you change behavior, update the doc in the same change.

## 3. Hard constraints (never violate)

- **Networking uses `socket` and `selectors` only.** No `asyncio` streams, `socketserver`, `websockets`, Flask/FastAPI, Socket.IO, ZeroMQ, or similar.
- **LAN only. No internet calls at runtime**: no CDNs, telemetry, online fonts, or update checks. Everything needed ships in the app, including fonts used in exports.
- **Server is authoritative.** Never trust client claims about role, seq, ownership, chat recipient, or sender.
- **No UI imports** (tkinter, Pillow) in `common/` or `server/`. `client/` must not import `server/`.
- **Never block the Tk UI thread** with network or disk I/O (saves, journal writes, and export file writes go to the IO worker).
- **Never touch Tk widgets from the network, discovery, or IO threads.** Hand work to the UI thread with `client.widgets.call_in_ui`.
- **All persistent writes go through the durable writer** (`Architecture.md` §12.1): atomic replace, `fsync`, checksums. Never `open(path, "w")` on a data file directly.
- **Python 3.11+, cross-platform** (Windows, Linux, macOS): use `pathlib`, no OS-specific paths or shell calls, handle line endings.
- Pin dependencies in `requirements.txt` (`pillow`, `cryptography`, `pytest`, `pyinstaller`). The UI is tkinter from the standard library; rendering and export use Pillow plus the built-in SVG and PDF writers; encryption and signatures use `cryptography`. No new dependency is needed for the revision-2 features.

## 4. Setup and commands

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m server.main --host 0.0.0.0 --port 47801      # run server (runs the integrity scan first)
python -m client.main                                  # run client (launcher lists boards on the network)
pytest -q                                              # all tests
pytest tests/test_acl.py -q                            # single module
python -m build.make --os $(uname)                     # PyInstaller build (see build/)
```

Network ports: **TCP 47801** (app), **UDP 47800** (discovery: announces and discover requests). Document any change.

## 5. Coding conventions

- Type hints everywhere; `dataclasses` for messages and model objects; `black` style (line length 100); `ruff` clean.
- **Comment the *why*** (the brief grades commented source): module docstring, class docstring, and comments on every non-obvious protocol, concurrency, or durability decision (e.g. why `fsync` the directory, why DM channel ids are hashed).
- Small functions; no global mutable state except explicit singletons in `server/loop.py`.
- Message types, error codes, and file-format versions are **constants in `common/protocol.py` / `common/fileformat.py`**, never string literals scattered in code.
- Logging via `logging` (levels: DEBUG frames, INFO lifecycle, WARNING rejected ops/chat and recovery repairs, ERROR bugs). No `print` in library code (the server's startup summary uses a dedicated logger).
- Errors on the wire use `ERROR`/`OP_REJECT`/`CHAT_REJECT`/`SAVE_REJECT` with a stable code, not free text alone.

## 6. Protocol change rules

Any change to framing, message types, op or chat schema, discovery payload, file formats, or ACL rules requires, in the **same change**:
1. Update `Architecture.md`.
2. Bump `PROTOCOL_VERSION` in `common/protocol.py` if wire-incompatible (currently **2**), or the `.lanvas`/snapshot `version` if file-incompatible.
3. Add or update tests (framing, ordering, ACL, chat, persistence as relevant).
4. Notify the team (the protocol owner approves).

## 7. Security rules (agents: be paranoid)

- Every incoming frame passes: size cap → state-machine check → role check → schema validation → rate limit.
- **Deny by default.** `PENDING`/`DENIED` connections get nothing (no snapshot, no chat, no chat history, no presence, no checkpoints).
- **Private messages go to exactly two fingerprints.** Route by the channel's fingerprints, never by username or by a client-supplied list. History requests are checked against the requester.
- Never log DM text at INFO or above; never include DMs in `.lanvas` exports, audit exports, or the AI assistant's context.
- Validate all numeric ranges, string lengths, array sizes, JSON depth. Reject unknown `kind`s and unknown chat channels.
- Discovery: never put room codes, member names, or content in announces; reply unicast and rate-limited.
- Never log private keys, invite tokens, passwords, or full room codes. Store invite tokens and room codes **hashed**. Never write a `.lanvas` password anywhere.
- Private identity key and local backups live only in the client's user config/data dir with `0600` permissions where supported.
- Add a test for every new permission-relevant path, including a **raw-socket bypass** attempt.

## 8. Testing requirements

- New server logic: unit test plus, where relevant, a multi-client simulation asserting **identical board hashes**.
- New op kind: tests for dedupe, ordering, undo/redo inverse, and role restrictions.
- New chat behavior: routing test with a third listening bot (nothing leaks), plus restart-persistence test.
- New persistence code: a crash-injection test with `FaultyFS` at every write step.
- New export format: open-and-compare test offscreen.
- Before declaring a feature done, run the **convergence test** and the ACL matrix test.
- UI code: provide a manual test checklist in the PR description (what to click, expected result).

## 9. Definition of done (per task)

- [ ] Works on at least two OSes (or noted as untested on one)
- [ ] Offline verified (internet disabled)
- [ ] Tests added and passing; no new lint errors
- [ ] Docs updated (`Architecture.md` / `Design.md` as needed)
- [ ] No new third-party dependency without team approval
- [ ] Commented per §5
- [ ] Belongs to the **current phase** (§11) and respects the Tier rules in `ProductContext.md`

## 10. Work breakdown and ownership

| Module | Owner role | Key files |
|---|---|---|
| Protocol & server core | Backend lead | `common/protocol.py`, `server/loop.py`, `oplog.py`, `chatstore.py` |
| Access control & security | Security lead | `server/acl.py`, `common/identity.py`, `common/fileformat.py` (signing, encryption), TLS |
| Persistence & recovery | Backend lead + Client-net lead | `server/persist.py`, `server/recovery.py`, `client/recovery.py` |
| Client networking & models | Client-net lead | `client/net.py`, `document.py`, `chatmodel.py` |
| Canvas, tools, panels, export UI | UI lead | `client/canvas/`, `client/panels/`, `client/export/`, `theme.py` |
| Packaging, docs, discovery | Release lead | `build/`, `server/discovery.py`, `client/discovery.py`, PDF/PPT |

Agents: stay inside the module you were assigned. Cross-module changes need a note to the owner.

## 11. Phase plan (build → test → gate → next phase)

The project is delivered as a sequence of phases. **There are no calendar dates.** The rules:

1. Only one phase is *active* at a time. Work for the active phase merges to `main`; work for a later phase may be spiked on a branch but **does not merge** until its phase is active.
2. A phase is finished when every item in its **Build** list is done **and** its **Gate** passes. The gate always includes the gates of **all earlier phases** (full regression), so a new phase can never quietly break an old one.
3. Passing a gate is recorded in `docs/test-log.md` (commit, machines/OS, IDs run, results). Then the next phase becomes active.
4. If a gate fails: fix, add regression tests, re-run the whole gate. Repeat until green. S1/S2 bugs found later reopen the phase they belong to.
5. Within a phase, owners work in parallel on their modules.

```
 ┌─► build ─► test ─► gate green? ──yes──► log it ─► next phase
 │                       │ no
 └────── fix + regression test ◄──┘
```

| Phase | Build | Gate (IDs from `Architecture.md` §14, plus all earlier gates) |
|---|---|---|
| **0. Foundations** | `common/protocol.py` (framing, constants), `model.py`, `fractional.py`, `identity.py`, `chat.py`, `fileformat.py` (canonical JSON, hash, sign, encrypt); `persist.py` durable writer; `scripts/check.py`; test harness skeleton. **Freeze `common/` at the end.** | FR-01..FR-06; L0 for model, fractional, identity, file format; SV-03 (writer only); XP-06; `check.py` green on Windows and Linux |
| **1. Server core and storage** | Selectors loop, connection state machine, ops + seq, broadcast, dedupe, resync/snapshots, chat store (room + DM channels, history, retention), checkpoints and `SAVE`, startup integrity scan | OR-01..OR-06, RC-02, RC-03, RC-05..RC-07, CT-01, CT-02, CT-04, CT-06..CT-10, CT-12, SV-01, SV-03..SV-06, SE-01..SE-04 (L1/L2 with bots) |
| **2. Access control** | ACL, owner approval flow, invites, roles, mute, rate limits, pending/denied isolation for ops **and** chat | AC-01..AC-12, CT-05, CT-11, SV-02 |
| **3. Client core and discovery** | `net.py`, `document.py`, minimal canvas (pen + rect), discovery (server announce + client `DiscoveryWorker`), launcher with live board list, join flow and request-access screen; first PyInstaller build | DS-01..DS-08, RC-01, XP-03 (basic: launch, discover, join, draw), XP-04; first L3 run |
| **4. Tier 1 complete** | Full Tier 1 tools, room chat + private messages UI with retained history, undo/redo, reconnect with outboxes, Ctrl+S (checkpoint + local signed copy), client recovery journal and Recover dialog, Open `.lanvas`, export PNG and `.lanvas` | UN-01..UN-04, RC-04, CT-03, SV-07..SV-10, EX-01, EX-05; `Design.md` §13.1 rows for Tier 1; clean L3 run on 3+ machines (Tier 1 exit, `Architecture.md` §14.7) |
| **5. Tier 2: Figma feel** | Select/transform, layers, properties, frames, stickies, arrows, images, comments; export JPEG/SVG/PDF with scopes; password-protected `.lanvas`; checkpoint browser + restore; create room from file; Packet Inspector; chaos simulator | UN-05, SV-11..SV-13, EX-02..EX-08, CH-01..CH-03, PF-01..PF-06, XP-01..XP-03; full `Design.md` §13.1; L3 run (Tier 2 exit) |
| **6. Tier 3 and Signature (optional)** | Only items from `ProductContext.md` §6 Tier 3 / Signature, chosen by the team, one at a time; each item gets its own tests | Each item's new tests + full regression + L3 run. **Skip this phase entirely if Phase 5 took too long.** |
| **7. Release** | Feature freeze. Packaging on each demo OS, technical PDF, PPT, videos, demo rehearsal, fresh-machine unzip test | Release candidate criteria (`Architecture.md` §14.7): all IDs green on all demo OSes, XP-04..XP-06, L4 acceptance (`ProductContext.md` §16), fresh-machine offline test by two people not on the build |

The submission deadline from the course brief is fixed; if time runs short, drop Phase 6 first, then reduce Phase 5 scope. Never skip a gate.

## 12. Do / Don't quick list

**Do**
- Keep the demo script in `ProductContext.md` §12 working at all times (for the parts built so far).
- Prefer boring, readable code over cleverness.
- Ask when the spec is ambiguous; write the assumption in the PR.

**Don't**
- Don't add features outside the active phase or the current Tier.
- Don't start the next phase before the current gate is green and logged.
- Don't weaken permission checks "temporarily".
- Don't hardcode IPs, usernames, or absolute paths.
- Don't commit keys, `data/`, local backups, `.venv/`, or build artifacts.
- Don't rewrite files you don't own; propose a patch instead.

## 13. Commit and review conventions

- Conventional commits: `feat(server): ...`, `fix(client): ...`, `docs: ...`, `test: ...`.
- Small PRs (< 400 lines when possible), one owner review before merge to `main`.
- Branch names: `feat/<area>-<short>`, `fix/<area>-<short>`; spikes for a later phase: `spike/p<N>-<short>`.
- Tag each passed gate: `phase-<N>-green`.
- `main` must always run the demo script (for the parts built so far).

## 14. Course deliverables checklist (don't forget)

- [ ] Server executable and client executable (per OS demoed)
- [ ] Commented source for both
- [ ] Technical documentation PDF (architecture, message format, sockets, discovery, chat, sync, concurrency, persistence and recovery, **user guide**)
- [ ] PPT with screenshots and workflow diagrams
- [ ] Explanation video: every member, face and name visible, explaining their own contribution
- [ ] Single zip; one member submits; **before the submission deadline in the brief**
- [ ] Live demo with internet disabled; every member can answer questions about their code

---

## 15. Testing workflow (how we test, who does what)

Full test catalogue and IDs: `Architecture.md` §14. This section is the day-to-day procedure.

**Commands**

```bash
python scripts/check.py                       # ruff + L0/L1/L2 fast set (run before every push; same on Windows and Linux)
pytest -m unit -q                             # L0
pytest -m integration -q                      # L2 (server + bot clients, localhost)
pytest -m "security or chaos" -q              # AC/SE/CH suites (before merge to main)
pytest -m "chat or discovery" -q              # CT/DS suites
pytest -m "persist or export" -q              # SV/EX suites (export tests run offscreen)
pytest -m perf -q                             # PF suite (Phase 5 gate onward)
pytest --seed 12345 -q                        # reproduce a randomized failure
python scripts/gate.py --phase 4              # run every ID for phases 0..4, print a gate report for test-log.md
```

**Rules**
1. Tests are **deterministic**: use the fake clock, seeded randomness, ephemeral ports, temp dirs. No `sleep()` waits longer than 100 ms in tests.
2. **Test first for hard parts:** framing, ordering, ACL, DM routing, undo, durable writes, recovery. Write the test (by catalogue ID), see it fail, then implement.
3. **Every fixed bug gets a regression test** with the bug's ID in the test name.
4. Tests run on **Windows and at least one Linux distro** before merge (see §16 matrix). A test that only passes on one OS is a bug.
5. Never put real keys, IPs, room codes, or passwords in tests; use generated fixtures.

**Rules specific to AI agents**
- Do **not** edit or delete a failing test, or weaken an assertion, to make it pass. If you believe the test is wrong, explain why and ask the module owner.
- When a test fails, report the **root cause** before proposing a fix; do not "fix" by adding retries or sleeps.
- Generate tests from the **catalogue IDs** in `Architecture.md` §14, and name them after the ID (`test_CT_02_dm_not_delivered_to_third_member`).
- After finishing a task, state which IDs you ran and their result, and which phase gate they belong to.

**Cadence (event-driven, no dates)**
| When | Who | What |
|---|---|---|
| Before each push | author | `python scripts/check.py` |
| Before merging to `main` | reviewer | full L0-L2 incl. security, chat, persist, and chaos |
| Once per working session, from Phase 3 on | whole team | L3 LAN test (`Architecture.md` §14.6, steps for features built so far) on 3+ machines with mixed OS; one person runs the checklist, another records results in `docs/test-log.md` |
| End of each phase | phase owner + QA | `scripts/gate.py --phase N` on Windows and Linux, plus L3 if the phase has GUI work; log and tag `phase-<N>-green` |
| Phase 3 gate | release lead | first PyInstaller build and XP-03 on every OS |
| Phase 7 gate | all; fresh-machine test by two people not on the build | full regression, acceptance (`ProductContext.md` §16), offline unzip test |

**Test log (`docs/test-log.md`)** one row per gate and per L3/L4 run: phase, commit, machines/OS, IDs run, pass/fail, bugs opened. This log also feeds the technical PDF and the PPT.

**Merge gate:** no merge to `main` with a red test, an S1/S2 bug open on that area, or the demo script broken.

---

## 16. Team and operating-system notes

Team size is 4 to 5, on a mix of Linux distros and Windows. Plan for both from Phase 0.

**Suggested OS spread for L3 runs:** at least one Windows machine and one Linux machine in every run; rotate who hosts the server so both OSes act as server and as client.

**Known cross-platform pitfalls (check each early):**
| Area | Windows | Linux |
|---|---|---|
| Firewall | Defender prompts on first listen; allow TCP 47801 and UDP 47800 (inbound **and** outbound broadcast) | `ufw`/`firewalld` may block; document the exact commands |
| UDP broadcast | Use `SO_BROADCAST`; enumerate interfaces, don't rely on `255.255.255.255` only | Same; also check multiple interfaces (Wi-Fi + Docker + VM bridges) |
| UDP port sharing (server and client on one machine) | `SO_REUSEADDR` allows sharing | Set `SO_REUSEADDR` (and `SO_REUSEPORT` if available) **before** `bind` on both sockets |
| Network change detection | Interface list polling every 5 s | Same |
| Tk | Included with the python.org installer | Install `python3-tk` (Debian/Ubuntu) or `python3-tkinter` (Fedora) when running from source |
| Atomic save | `os.replace` fails if the target is open in another program (antivirus, viewer): retry a few times, then report "Save failed" | `os.replace` is atomic; also `fsync` the directory |
| Paths/perms | Use `pathlib` and `QStandardPaths`; `0600` key and backup permissions are best-effort | Enforce `0600` on the identity key and local backups |
| Line endings | Normalize to `\n` in saved files | n/a |
| Packaging | Build `.exe` on Windows; include Tk and Pillow's Tk bridge (`PIL.ImageTk`) | Build on the oldest distro you target (glibc compatibility), one binary per distro family if needed |
| Fonts | Bundle Inter; never rely on system fonts (exports too) | Same |
| Time | Server stamps messages in UTC; client formats in local time | Same |

**Roles for 5 people** (with 4, the QA/release role is shared between roles 1 and 3):
1. Protocol & server core, chat store, server persistence
2. Security, ACL, audit log, file signing/encryption
3. Client networking, document and chat models, undo, offline queue, client recovery journal
4. Canvas, tools, panels, chat UI, export (UI)
5. QA, packaging, discovery, docs, AI assistant integration; owns `scripts/check.py`, `scripts/gate.py`, and `docs/test-log.md`
