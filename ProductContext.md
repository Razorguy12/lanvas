# ProductContext.md

> Working name: **LANvas** (rename freely). Single source of truth for *what we are building and why*.
> See also: `Architecture.md` (how), `Design.md` (look and feel), `Agents.md` (rules for AI/code contributors and the phase plan).

---

## 1. One-line pitch

A **Figma-style, offline, LAN-only collaborative design whiteboard** where the room creator controls exactly who can view or edit, boards on the network show up by themselves, nothing is lost on a crash, and the network protocol itself is visible and demonstrable.

## 2. Context

- **Course:** Computer Networks (CN) Lab project, LAN-Based Multi-User Collaborative Whiteboard.
- **Submission:** fixed by the course (see the brief); no extensions. In-person demo with the internet disabled.
- **How we work:** the project is built **phase by phase** (`Agents.md` §11). A phase starts only after the previous phase is finished **and** its tests pass; the cycle repeats until the whole project is complete and tested. There are no internal calendar milestones.
- **Constraints from the brief:** server-based, client-server over TCP/IP sockets, LAN only, no internet; low-level socket programming is mandatory; Python recommended; standalone server and client executables; PDF docs, PPT, per-member video.
- **How we are graded:** every team has the same core. Marks go to **completeness, correctness, and extra features with effective integration**. Our extras must feel like one coherent product, not a pile of add-ons.

## 3. Problem and users

| User | Need | How we serve it |
|---|---|---|
| Student groups in a lab or hostel | Brainstorm and sketch together without internet; talk privately while working | Live board discovery, fast join, room chat and private messages |
| Instructor / host | Control who is in the room, protect the board, never lose work | Owner-approved access, roles, kick, lock; Ctrl+S checkpoints; crash recovery |
| Evaluator (grader) | See networking concepts working, quickly | Packet Inspector, chaos simulator, clean docs |

## 4. Product principles

1. **Offline-first, LAN-only.** Nothing ever touches the internet.
2. **Creator is in control.** Knowing the room code or being on the same Wi-Fi is *not* authorization.
3. **The board is always consistent.** Server-authoritative ordering; any client can be dropped and recover.
4. **Nothing is lost.** Every write is atomic and checksummed; a crash of the client or server recovers to the last good state.
5. **The protocol is a feature.** We expose it, we do not hide it.
6. **Figma-lite, done well** beats Figma-everything done badly.

## 5. Differentiators (our "unique angle")

1. **Creator-gated access with a request-to-join flow** (Section 7).
2. **Figma-style object canvas**: shapes, frames, layers, properties panel, multiplayer cursors, sticky notes.
3. **Live board discovery**: a device that joins the network sees the available boards in its launcher within seconds (only board names are advertised, never content or codes).
4. **Room chat plus private messages**, both kept as history across reconnects and server restarts.
5. **Secure save and recovery**: Ctrl+S makes a verified server checkpoint; a local recovery journal supports crash recovery.
6. **Export anywhere**: PNG, JPEG, SVG, and PDF.
7. **Live Packet Inspector** showing every frame, sequence number, size, and latency.
8. **Chaos / Network Simulator** (delay, duplicate, reorder, drop connection) to prove ordering and recovery live.
9. **Version history** built on the append-only operation log and checkpoints.

## 6. Feature scope (tiered)

Tiers describe *what*; the phases in `Agents.md` §11 describe *in which order* it is built and tested.

### Tier 1: mandatory core (Phases 0 to 4)
- Pen (freehand), eraser, color selection on a shared canvas
- Rectangle, ellipse, line, text (these also serve the "extended" credit)
- Real-time sync, consistent shared state, server-side validation and ordering
- Host a room; **live list of available boards** on the network; join from the list or with LAN address plus room code; participant list with join/leave notices
- Usernames; per-user color; live cursors with name labels
- **Group chat with sender and timestamp, plus private messages between members; chat history retained**
- New joiner receives the current board and recent chat; reconnect recovers the latest state
- Undo / redo with defined multi-user behavior
- Clear canvas (permission required)
- **Save with Ctrl+S** (server checkpoint), crash recovery on server and client
- **Export:** PNG
- **Access control: owner-approved membership** (this is Tier 1 for us, not optional)

### Tier 2: the Figma feel (Phase 5)
- Select, move, resize, rotate, multi-select; z-order; layers panel; properties panel
- Zoom, pan, snap guides, align tools, duplicate, group
- Frames, sticky notes, arrows, image insertion
- **Export:** JPEG, SVG, PDF (including "each frame as a page"), scope = board / selection / frame
- Checkpoint browser and restore (owner)
- Packet Inspector; chaos simulator

### Tier 3: stretch (Phase 6, only if Tiers 1 and 2 pass multi-machine tests)
- Version history timeline slider, presentation (follow-me) mode
- TLS transport, per-page permissions, dark mode polish

> **Rule:** Tier 3 work does not begin until Tier 1 passes a 4-machine LAN test.

### Signature tier: "our style" (Phase 6, LLM-accelerated scope, only on a green Tier 1 + Tier 2)
Identity: **a Figma-class canvas that is network-transparent and security-native.**
- **Offline AI assistant (local LLM via Ollama on the LAN host):** "draw a login-flow flowchart", "tidy these notes", "summarize chat and board". The assistant joins as a visible participant with its own role, and its ops pass through the *same* server validation and ACL as any user. It never reads DMs. No internet; optional and fails gracefully if no model is present.
- **Smart connectors:** lines/arrows bound to shape anchors that follow when shapes move (flowcharts, mind maps).
- **Audit log panel (owner):** who did what, when, from which IP/fingerprint; filterable; exportable.
- **Local-first offline edits:** keep editing while disconnected, then merge on reconnect via the op log.
- **Branchable version history:** scrub the timeline, fork a version into a new room.
- **Lite components and auto-layout:** reusable symbols, row/column auto layout for frames.
- **Presentation / follow-me mode:** owner drives everyone's viewport.

## 7. Access control (key requirement)

**Statement:** Only people authorized by the room creator can view or edit the board, or read its chat, even if they are on the same network. Unauthorized people can *request* permission.

**Roles:** `owner` (creator), `editor`, `viewer`, and no access.

**User stories**
- As an owner, I create a room and become its only initial member.
- As a visitor, I open the app and see the boards available on my network; if I pick one (or connect with a room code) but am not authorized, I see only a "Request access" screen. I receive **no** board data, chat, or participant list.
- As an owner, I get a prompt "Alice (192.168.1.23, fingerprint `7F3A-91C2`) requests access", and I choose Approve as Editor, Approve as Viewer, or Deny.
- As an owner, I can pre-invite people with a single-use invite code, change anyone's role, revoke access, mute, or kick, at any time.
- As an approved member, I reconnect later and am recognized automatically (cryptographic identity), without asking again.
- As a viewer, I see live updates but my edit attempts are rejected by the **server** (not just greyed out in the UI).

**Acceptance criteria**
- A raw socket client that skips the UI and sends `OP` or `CHAT` frames without approval is rejected and cannot read state or chat.
- Requests are rate-limited; repeated denied requests are blocked for a cool-down.
- ACL survives server restart.

## 8. Chat and private messages

**User stories**
- As a member, I chat with the whole room; each message shows sender, color, and time.
- As a member, I start a **private conversation** with any other member (from the People list or by clicking a name in chat). Only the two of us receive it.
- As a member, if I message someone who is offline, they get it, with an unread badge, when they next connect.
- As a member, when I reconnect or come back the next day, the room chat and my private conversations are still there; I can scroll back through older messages.
- As an owner, I can set how long chat is kept (forever, last N messages, last N days), clear the room chat, and mute a member (mute covers room chat and private messages).

**Acceptance criteria**
- A third member, or a raw socket client, never receives a private message that is not theirs (CT-02, CT-07).
- Room chat and private messages, with order, timestamps, and unread positions, survive a server restart (CT-08).
- Pending and denied users cannot send or read any chat (CT-05).
- Private messages are never shown in someone else's Packet Inspector.

**Honest limit:** private messages are private from other members, not from whoever controls the server machine; they are not end-to-end encrypted (see §9 non-goals).

## 9. Non-goals

- Internet or cloud sync, accounts, or cloud storage
- Mobile apps
- Full Figma parity (components, auto layout, prototyping, plugins, vector boolean ops)
- Strong anonymity or end-to-end encryption (including of private messages) beyond the LAN transport protections described in `Architecture.md`
- Preventing a viewer with a modified client from capturing a board they are allowed to see

## 10. Save, recovery, and export (product rules)

- **Ctrl+S saves a verified server checkpoint.** If the server is unreachable, the checkpoint request follows on reconnect. The local recovery journal preserves unsent work after a crash.
- **A crash never costs more than the last few hundred milliseconds of edits.** The server repairs itself on restart; the client offers a Recover dialog with unsent changes.
- **Every export opens offline** on another machine and OS: PNG, JPEG, SVG, PDF.
## 11. Success criteria

- Demo runs from the downloaded zip on a fresh machine, offline, in under 3 minutes of setup.
- A newly connected device sees the available boards without typing an address.
- 4+ clients draw simultaneously with no divergence (verified by board hash comparison).
- Kill a client's connection mid-stroke; on reconnect the board matches the server.
- Kill the server process; on restart the board, room chat, and private messages are intact.
- An unapproved client can reach the request screen but cannot see or change anything.
- Each team member can explain their module's code and design choices.

## 12. Demo script (about 8 minutes, rehearse it)

1. Open Client B's launcher first (empty: "Looking for boards…"). Start the server. Client A creates room "Sprint-1" (owner). **Sprint-1 appears on B by itself.**
2. B clicks Sprint-1; sees **Request access** only.
3. Owner approves B as Editor. B's board and recent chat appear instantly (snapshot).
4. Client C joins as Viewer; tries to edit; server rejects.
5. A, B draw and move shapes simultaneously; show cursors, layers, room chat. B sends A a **private message**; C sees nothing.
6. Open the **Packet Inspector**; point out seq numbers and acks; the DM shows as «private» on C.
7. Enable **Chaos mode** (delay and duplicates); show the board stays consistent.
8. Kill B's Wi-Fi; reconnect; show resync from `last_seq`.
9. Undo/redo with two users; show conflict-aware behavior.
10. A presses **Ctrl+S**: "Saved · checkpoint" with matching hash. **Kill the server process**, restart it: board, chat, and DMs are back.
11. Export **PDF** and **PNG**; open them on another machine. Show version history (if built).

## 13. Rubric mapping

| Rubric item | Our answer | Doc |
|---|---|---|
| Network architecture | Central server, raw TCP sockets, UDP discovery, offline | Architecture §2, §4.4 |
| Protocol design | Framed binary header + JSON, TCP vs UDP justified, chat channels | Architecture §4 |
| User interface | PyQt6 Figma-like UI | Design.md |
| Performance | Batching, simplification, throttled cursors, batched fsync, stats | Architecture §9 |
| Concurrency | Single-threaded selector loop, global seq, per-channel chat seq | Architecture §5, §4.5 |
| Reliability / recovery | Atomic checksummed storage, integrity scan, client journal | Architecture §12 |
| Security / access | Owner-approved ACL, signed identity, DM routing, rate limits | Architecture §8 |
| Extras | Discovery, private chat, export formats, chaos sim, inspector, history | §5 above |

## 14. Risks

| Risk | Mitigation |
|---|---|
| Figma-like scope explodes | Tiering and phase gates; QGraphicsScene gives selection/zoom/pan nearly free; feature freeze at the Phase 7 gate |
| Demo-day firewall or Wi-Fi isolation (blocks discovery) | Pre-test on router or phone hotspot; server prints IPs; join-by-address fallback; fixed ports documented |
| UDP port sharing when server and client run on one machine | `SO_REUSEADDR`/`SO_REUSEPORT`; DS-05 on both OSes early (Phase 3) |
| `fsync`/atomic replace behaves differently on Windows | Shared durable writer; SV-03 and XP-06 on both OSes |
| Qt packaging issues | First PyInstaller build at the Phase 3 gate, not at the end |
| Team skill gaps in Qt | Pair UI owner with protocol owner; shared `common/` freezes in Phase 0 |

## 15. Glossary

- **Op:** one atomic change to the board (create, update, delete).
- **Seq:** server-assigned global order number for each accepted op.
- **Channel:** a chat stream: the room channel or a DM channel between two members; each has its own `chat_seq`.
- **DM:** a private message between two members of the same room.
- **Snapshot:** full serialized board state at a given seq (automatic).
- **Checkpoint:** a snapshot made on Ctrl+S, kept until the owner deletes it, restorable.
- **Recovery journal:** client-side file of the last confirmed board plus unsent changes, used after a crash.
- **ACL:** access control list for a room (identity to role).
- **Identity:** a client keypair; its public-key fingerprint is the user's stable ID.
- **Phase gate:** the set of tests that must pass before the next phase starts (`Agents.md` §11).

---

## 16. Acceptance test plan (maps the brief to evidence)

Each row must be **demonstrable offline on the demo hardware**. IDs refer to `Architecture.md` §14. "Evidence" is what we screenshot or record for the PDF/PPT.

| # | Requirement | Verified by | Pass criteria | Evidence |
|---|---|---|---|---|
| R1 | Freehand draw, erase, color | manual + UI checklist (`Design.md` §13) | Strokes appear for all; eraser removes whole strokes; color changes sync | screenshot |
| R2 | Real-time sync, minimal delay | PF-01, L3 | median < 50 ms, p95 < 150 ms on LAN | inspector screenshot |
| R3 | Server validates and distributes | SE-04, OR-01 | invalid ops rejected; valid ops reach all | log excerpt |
| R4 | Shared consistent canvas | OR-01, hash check | all hashes equal | status-bar hash on every machine |
| R5 | Clear canvas with permission; save and restore | AC-01, SV-01, SV-11, RC-05 | only permitted roles clear; Ctrl+S hash matches; restore matches hash | video clip |
| R6 | Host session; join by IP + room code | L3 step 5 | join works from each OS | screenshot |
| R7 | Participant list; join/leave notices | manual | notices on join, leave, away | screenshot |
| R8 | Usernames; per-user visual distinction | manual | colored cursors, name tags | screenshot |
| R9 | Join/leave without disruption; new joiner gets state | RC-03, L3 | existing users unaffected; newcomer sees full board and recent chat | video clip |
| R10 | Operations over sockets; broadcast | FR-*, OR-* | raw TCP/UDP only, no web libs | code review + inspector |
| R11 | Concurrent editing without lost updates | OR-01, OR-05, CH-01 | converge under load and chaos | test report |
| R12 | Ordering and reliability (delayed/duplicate/missing) | OR-02, OR-03, CH-02, RC-02 | documented rules hold | chaos demo |
| R13 | Disconnect detection, timeouts, reconnection | RC-01, RC-06 | session survives; client resumes | video clip |
| R14 | Group chat with sender and timestamp | CT-01, manual | chronological, correct sender/time | screenshot |
| R15 | State representation, sync, recovery | RC-03, RC-05 | snapshot + replay correct | test report |
| R16 | Undo/redo with defined multi-user behavior | UN-01..UN-05 | matches `Architecture.md` §5.5 | demo with two users |
| R17 | Access control, no unauthorized modification | AC-01..AC-12, SE-* | unapproved users see and change nothing | raw-socket bypass demo |
| R18 | LAN only, no internet | L3 offline run, EX-07 | works with internet disabled | demo-day step |
| R19 | Standalone executables | XP-03 | launch from the zip on a clean machine | fresh-machine log |
| R20 | Source, PDF, PPT, videos, single zip | `Agents.md` §14 checklist | all present before submission | zip listing |
| R21 | Private messages between users | CT-02..CT-07, CT-11 | only the two members receive them | demo step 5 + raw-socket test report |
| R22 | Chat history retained | CT-08..CT-10 | room chat and DMs survive reconnect and server restart | video clip |
| R23 | Available boards shown when a device joins the network | DS-01..DS-08 | board appears without typing an address; nothing private advertised | screen recording |
| R24 | Export PNG, JPEG, SVG, PDF | EX-01..EX-08 | every file opens on another OS offline | exported files |
| R25 | Secure save and file recovery | SV-01..SV-07, SV-11 | Ctrl+S verified; crash recovery and checkpoint restore work | test report + demo step 10 |

### Demo-day checklist
- [ ] Router/hotspot with internet off; machine IPs noted
- [ ] Firewall rules applied (TCP 47801, UDP 47800, both directions) on server and clients
- [ ] Zip downloaded from the submission and unpacked on the demo machine
- [ ] Client launchers open first; server started; the room appears by itself
- [ ] Owner, editor, viewer, and a rejected visitor ready
- [ ] Chaos mode and inspector tested beforehand
- [ ] Backup: second laptop with the same build; exported PDF; screenshots of the test log
- [ ] Every member knows their module and can answer questions about it
