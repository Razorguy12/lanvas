# Design.md

> UI/UX design system for LANvas (tkinter). Goal: **feels like Figma**: calm, dense, tool-focused, with the canvas as the hero.
> Implement tokens in `client/theme.py`; style with ttk styles and custom-painted widgets (Tk canvases, Pillow images) where needed.

---

## 1. Design principles

1. **Canvas first.** Chrome is quiet; the board gets the pixels.
2. **Direct manipulation.** Select, drag, resize, rotate on canvas; panels refine.
3. **Presence is visible.** Other people's cursors, selections, and edits are always legible.
4. **Permission is visible.** Your role is always shown; disabled actions explain *why*.
5. **Privacy is visible.** Private conversations always say who can read them.
6. **Saved state is visible.** You can always tell whether your work is safe.
7. **Keyboard-friendly.** Every tool has a single-key shortcut, as in Figma.

## 2. Window layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Top bar: ≡ Room name ▾ │ Share/Access │ avatars (live) │ zoom % │ ⚙ Inspector│
├──────┬───────────────┬───────────────────────────────────────────┬───────────┤
│Tool  │ Left panel    │                                           │ Right     │
│rail  │ • Layers      │              CANVAS                       │ panel     │
│(v)   │ • Pages/Frames│        (infinite, zoom/pan)               │ • Design  │
│      │               │                                           │ • Comments│
│      │               │                                           │ • Chat    │
├──────┴───────────────┴───────────────────────────────────────────┴───────────┤
│ Status bar: role • connection • RTT • seq • objects • save state ("Saved 21:47")│
└──────────────────────────────────────────────────────────────────────────────┘
```

- Panels are collapsible (`Ctrl+\` toggles all UI, as in Figma).
- Floating **bottom-center toolbar** is optional; default is the vertical left tool rail for simplicity of implementation.
- The **≡ Room name ▾** menu contains: Save (`Ctrl+S`), Save a copy… (`Ctrl+Shift+S`), Open `.lanvas`…, Export… (`Ctrl+Shift+E`), Version history, **Switch board ▸** (live list of other boards on the network), Leave room.

## 3. Tokens

### 3.1 Color (light and dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `bg.app` | `#F5F5F5` | `#1E1E1E` | window |
| `bg.panel` | `#FFFFFF` | `#2C2C2C` | panels |
| `bg.canvas` | `#E5E5E5` | `#1A1A1A` | canvas backdrop |
| `border` | `#E6E6E6` | `#3C3C3C` | dividers |
| `text.primary` | `#1E1E1E` | `#F5F5F5` | text |
| `text.muted` | `#6B6B6B` | `#A0A0A0` | labels |
| `accent` | `#0D99FF` | `#0D99FF` | selection, focus, primary buttons |
| `success` | `#14AE5C` | `#14AE5C` | connected, approved, saved |
| `warn` | `#FFA629` | `#FFA629` | reconnecting, pending, unsaved |
| `danger` | `#F24822` | `#F24822` | disconnected, delete, deny, save failed |
| `private` | `#9747FF` | `#B98AFF` | private-message lock icon and DM header strip |

**User colors** (assigned per participant, high contrast, colorblind-aware order):
`#F24822 #0D99FF #14AE5C #9747FF #FFA629 #FF4DA6 #00B8A9 #7A5C00`.
Each user color is used for their cursor, name tag, selection outline, and chat name.

### 3.2 Typography
- UI font: **Inter** (bundled), fallback `Segoe UI`, `SF Pro`, `Noto Sans`. Exports embed or use the bundled font only.
- Sizes: 11 px (labels), 12 px (body/panels), 13 px (headers), 11 px semi-bold (section titles, uppercase).

### 3.3 Spacing, shape, elevation
- 4 px base grid (4/8/12/16/24).
- Radius: 4 px controls, 6 px popovers, 2 px selection handles.
- Panel width: left 240 px, right 280 px (resizable, min 200).
- Popovers: 1 px border plus soft shadow (`0 4px 16px rgba(0,0,0,.18)`).

### 3.4 Iconography
Monochrome line icons (Lucide or Phosphor SVG set, bundled), 16 px, 1.5 px stroke. Rasterized from inline SVG path data with Pillow (`client/icons.py`). Lock icon marks private conversations; shield-check marks signed files.

## 4. Tools and shortcuts

| Tool | Key | Notes |
|---|---|---|
| Move / Select | `V` | click, shift-click, marquee; handles to resize/rotate |
| Frame | `F` | container that clips and groups |
| Rectangle | `R` | `Shift` = square |
| Ellipse | `O` | `Shift` = circle |
| Line / Arrow | `L` / `Shift+L` | `Shift` snaps to 15° |
| **Pen (freehand)** | `P` | creates `path`; smoothing |
| **Eraser** | `E` | deletes whole strokes under cursor |
| Text | `T` | click to type; double-click to edit |
| Sticky | `S` | |
| Comment | `C` | pin comment, threads |
| Hand | `Space` (hold) | pan |
| Zoom | `Ctrl+scroll`, `Ctrl+0` (100%), `Shift+1` (fit) | |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Shift+Z` | per-user |
| Duplicate / Group / Delete | `Ctrl+D` / `Ctrl+G` / `Del` | |
| Bring forward / back | `Ctrl+]` / `Ctrl+[` | |
| Align | `Alt+A/D/W/S/H/V` | left/right/top/bottom/centers |
| **Save** | `Ctrl+S` | server checkpoint + signed local copy |
| **Save a copy…** | `Ctrl+Shift+S` | `.lanvas` to a chosen location, optional password |
| **Open `.lanvas`** | `Ctrl+O` | view, or create a room from it (owner) |
| **Export…** | `Ctrl+Shift+E` | PNG, JPEG, SVG, PDF, `.lanvas` |
| Chat / new private message | `Ctrl+Shift+C` / `Ctrl+Shift+M` | focus chat input / pick a member |
| Toggle UI | `Ctrl+\` | |

## 5. Canvas behavior

- **Infinite canvas** with smooth zoom (25–400%+) and pan (space-drag, middle mouse, trackpad).
- **Selection:** 1 px accent outline plus 8 resize handles plus rotation zone at corners; multi-select shows a group bounding box.
- **Snap guides:** magenta lines when aligning with neighbors, plus distance labels (stretch).
- **Remote presence:**
  - Cursor arrow in the user's color with a name tag (rounded, white text).
  - Remote selections: 1.5 px outline in that user's color with a small name chip.
  - Live stroke preview appears as the remote user draws.
- **Locked objects** show a lock glyph; moving is blocked.
- **Viewers** get a cursor with an eye icon; edit tools are disabled with tooltip "Viewers can't edit. Ask the owner."

## 6. Panels

### Layers (left)
Tree view with type icon, name, eye (visibility), lock; drag to reorder (writes fractional `z`); rename on double-click; click selects; colored dot when someone else has it selected.

### Design (right)
Contextual sections: Position & size (X, Y, W, H, rotation), Fill, Stroke, Opacity, Text (font size, weight), Alignment buttons, Export (quick "Export selection" shortcut into the Export dialog). Multi-selection shows mixed values as "Mixed". Numeric fields support drag-to-scrub.

### Chat (room + private messages)

```
┌ Chat ─────────────────────────── [✎ New] ┐
│ # Sprint-1 (room)                    •3  │  ← channel list (collapsible)
│ 🔒 Bob                                •1  │
│ 🔒 Chitra                                 │
├──────────────────────────────────────────┤
│ 🔒 Private · only you and Bob can read    │  ← header strip (DMs only, `private` color)
│        ── Load older messages ──          │
│ ───────── Yesterday ─────────             │
│ Bob 18:02  can you fix the arrow?         │
│                     ok, on it  18:03 You  │
├──────────────────────────────────────────┤
│ [ Message Bob…                    ] Send  │
└──────────────────────────────────────────┘
```

- **Channel list** at the top: the room channel first, then private conversations sorted by latest message; each shows an unread badge. The tab badge shows the total unread.
- **Starting a private message:** `✎ New` → member picker; or **Message** in the People popover; or click a sender's name in chat → "Message privately". Members are shown by name with a short fingerprint when two names collide.
- **Messages:** grouped by sender with user-color name, timestamp (HH:MM), own messages right-aligned, day separators (Today / Yesterday / date). Scrolling to the top loads older history (spinner, then "Beginning of conversation").
- **Retained history:** the room chat and private conversations are already filled when you join or reconnect; nothing to restore by hand.
- **Offline recipient:** the header shows "Bob is offline; they'll see this when they reconnect."
- **States per message:** sending (faded), sent (normal), failed (red ⟳ retry). Unsent messages survive reconnects and crashes.
- **Roles:** viewers can read and, if not muted, send room messages and private messages. Muted users see the input disabled with "The owner has muted you in this room."
- **Owner extras** (⋯ menu on the room channel): Clear room chat (confirm dialog), Chat retention (Forever / Last N messages / Last N days).

### Comments
Pins on canvas; thread list in panel; resolve toggle.

### Users and Access
Avatar stack in the top bar (initial letters on user color). Click opens the **People popover**: list with role badges, connection state, **Message** (opens a private conversation), "Following" option, and (owner only) role dropdown, kick, mute.

## 7. Access-control UX (key flow)

### 7.1 Visitor: "Request access" screen
Shown when the server returns `ACCESS_PENDING`. Contains: room name, owner name, your fingerprint (short, for verbal verification), a **Request sent, waiting for approval…** state with a subtle spinner, and **Cancel**. No board, chat, or user list is rendered; the board area shows a blurred placeholder illustration, not real data.

States: `Waiting` (amber) → `Approved` (green, auto-transition into the board) / `Denied` (red, with retry cool-down timer).

### 7.2 Owner: access request toast and dialog
Toast (top right): "**Alice** wants to join Sprint-1" with `Review`.
Dialog: name, IP, fingerprint short code (e.g. `7F3A-91C2`), buttons **Approve as Editor**, **Approve as Viewer**, **Deny**, and "Block this device". The owner is encouraged to confirm the fingerprint verbally. Pending requests also appear as a badge on the Share button.

### 7.3 Share / Access panel (owner)
- Room name, room code (copy), LAN address(es) (copy), **Discoverable on this network** toggle (off = hidden), "Require room code to request access", "Show member count in discovery".
- **Create invite** (role, expiry, single-use), shown as a short code.
- Member table: role dropdown, Mute, Revoke, Kick.
- Room settings: lock board (read-only for all but owner), "Editors can clear canvas", "Viewers can export and save copies", "Editors can approve requests" (optional).

### 7.4 Role indicators
Status bar chip: `OWNER`, `EDITOR`, `VIEWER` with distinct colors. Role changes show a toast ("You are now an editor").

## 8. Dialogs and flows

### 8.1 Launcher (first screen): boards on this network

```
┌ LANvas ───────────────────────────────────────────────────────────┐
│ Your name [ Azhar        ]                                        │
│                                                                   │
│ BOARDS ON THIS NETWORK                  ● live   [⟳ Refresh]      │
│ ┌───────────────────────────────────────────────────────────────┐ │
│ │ Sprint-1        Lab-PC-3 · Alice   3 people   🔒 approval   [Join]│
│ │ OS-Assignment   Ravi-Laptop        —          🔒 approval   [Join]│
│ │ Retro (needs update)  v1 server    —          —            (greyed)│
│ └───────────────────────────────────────────────────────────────┘ │
│ Join by address…   Create room…   Open .lanvas…                   │
└───────────────────────────────────────────────────────────────────┘
```

- The list fills **by itself** as soon as the device is on the network: new boards slide in, boards that disappear fade out after a few seconds. A pulsing **● live** dot shows that listening is active; `Refresh` forces an immediate search.
- Columns: board name, host machine and owner name, member count (only if the owner allows), lock icon meaning "needs owner approval".
- Click a row or **Join** → request flow (§7.1). If the room requires a code, a small code field appears inline first.
- **Empty state:** "Looking for boards on your network…" with spinner; after 10 s, add hints: "Make sure you're on the same Wi-Fi. Allow LANvas through your firewall (UDP 47800). Or **Join by address**."
- **Join by address** (IP, port, room code) remains for hidden rooms and isolated networks.
- Username field remembered locally.
- **Recover banner:** if the last session crashed, a banner at the top opens the Recover dialog (§8.6).

### 8.2 Create room
Name, code (auto-generated, editable), discoverable toggle (default on), "Start from a `.lanvas` file…" (optional).

### 8.3 Save (Ctrl+S) and save state

- `Ctrl+S` gives immediate feedback in the status bar: `Saving…` (spinner) → `Saved · 21:47` (green check, tooltip: checkpoint seq, board hash, local copy path) or `Saved locally · server checkpoint pending` (amber) or `Save failed: <reason>` (red, click to retry).
- Between saves, the status bar shows `Edited since last save` in muted text. No modal dialogs on Ctrl+S.
- Viewers without export permission: toast "Viewers can't save copies in this room."
- **Save a copy… (`Ctrl+Shift+S`)**: native file dialog plus options: include room chat (off by default; private messages are never included), **Protect with password** (password + confirm, strength hint, "If you forget it, the file cannot be opened").

### 8.4 Export (`Ctrl+Shift+E`)

```
┌ Export ──────────────────────────────────────────┐
│ Format   (•) PNG  ( ) JPEG  ( ) SVG  ( ) PDF  ( ) .lanvas │
│ What     (•) Whole board ( ) Selection ( ) Frame [▾]│
│ PNG/JPEG scale [1x ▾]   Background (•) Canvas ( ) Transparent │
│ PDF page  [Fit to content ▾]  [ ] Each frame as a page │
│ .lanvas   [ ] Include room chat  [ ] Protect with password │
│ Estimated size: 2400 × 1600 px · ~1.2 MB          │
│                              [Cancel] [Export…]   │
└──────────────────────────────────────────────────┘
```

- Only options relevant to the chosen format are enabled.
- Large exports show a progress bar; the UI stays usable. Success toast: "Exported Sprint-1.pdf · Show in folder".

### 8.5 Open `.lanvas`
Shows a trust header before anything else: `✔ Signed by Alice (7F3A-91C2)` (green) or `⚠ This file was modified or is unsigned` (amber) or a password prompt for protected files. Actions: View (read-only), Create room from this file (owner), Cancel.

### 8.6 Recover dialog (after a crash)
"LANvas didn't close properly last time. **Sprint-1** has 14 unsaved changes and 1 unsent message." Buttons: **Rejoin and send my changes** (primary), **Save recovered board as .lanvas…**, **Discard**. After rejoining, a toast reports "14 changes recovered."

### 8.7 Version history (Tier 2 restore, Tier 3 timeline)
List of checkpoints (time, author color, label, hash short code); "Restore this version" (owner, confirm dialog, undoable). Tier 3 adds the timeline slider with author-colored ticks.

## 9. Network visibility widgets

- **Connection dot** (status bar): green = live, amber = reconnecting, red = offline. Tooltip shows RTT.
- **Packet Inspector** (drawer, `Ctrl+Shift+I`): table columns `Time | Dir | Type | Seq | Size | RTT`, filter by type (incl. `CHAT`, `SAVE`, discovery), pause/resume, row details show decoded JSON (private-message text shown only to its two participants, otherwise `«private»`), and a mini sparkline of msgs/s. A **Discovery** tab lists received announces (source IP, rooms, age).
- **Chaos panel** (inside inspector): sliders for added latency (0–1000 ms) and duplicate probability, buttons "Drop connection" and "Reorder next 5". Clearly marked **Debug**.

## 10. States and feedback

| State | Treatment |
|---|---|
| Reconnecting | Banner "Reconnecting… your changes are saved locally", editing and chat input stay enabled |
| Op rejected | Toast with reason ("Viewers can't edit"); local change rolled back |
| Undo skipped | Toast "1 change not undone: edited by Sam" |
| Empty board | Subtle hint "Press P to draw, R for a shape" |
| Large paste/image | Progress indicator, size cap message |
| Saving / saved / pending / failed | Status-bar save chip (§8.3) |
| Save hash mismatch | Amber toast "Your view was out of date and has been refreshed", automatic resync |
| Recovered after crash | Toast "14 changes recovered" |
| New private message (chat closed) | Toast "🔒 Bob: can you fix…" with `Reply`; unread badge |
| Message failed | Red ⟳ on the bubble; click to retry |
| Board appeared / disappeared (launcher, Switch board) | Row slides in / fades out; no toast |

## 11. Accessibility and polish

- Minimum contrast 4.5:1 for text; focus ring (2 px accent) on all controls.
- Do not rely on color alone: role badges have text, user cursors have name tags, private conversations have a lock icon and the words "Private", save state has text.
- Scalable UI (respect OS DPI via Tk scaling), keyboard navigation of panels, launcher list and dialogs.
- Animations: 120–160 ms ease-out for popovers, toasts, and launcher rows; none on canvas manipulation.

## 12. Implementation notes (tkinter)

- Canvas: `CanvasView` (a Tk canvas) over a `BoardScene` of `BoardItem`s. The document is painted by the raster painter into one image (1x while panning or dragging, 2x supersampled once idle); selection handles, guides, previews and remote cursors are Tk canvas items on top.
- Remote cursors and selection overlays: a separate layer item, updated from signals, never part of the document, and **hidden during export**.
- Eraser hit-testing: `scene.items(path)` against stroke shapes with a small tolerance.
- Export: one renderer shared by all formats, painting each scene item directly in z-order through one painter API into a Pillow image (PNG/JPEG), the SVG writer, or the PDF writer (one page per frame). It is a generator that yields every 50 items, so large exports stay responsive and report progress. File writing runs on the IO worker. Overlays are Tk canvas items, never scene items, so they cannot reach an export.
- Chat: a `QTextBrowser` rendering escaped HTML per channel; lazy history loading on scroll-top. Chosen over a `QListView` delegate for grouping and right alignment at far less code; every piece of user text is `html.escape`d first.
- Launcher board list: `QAbstractListModel` fed by the `DiscoveryWorker` signal; diff-based updates so rows animate instead of flickering.
- Dark/light: one `Theme` dataclass; apply via `QPalette` plus QSS; switch at runtime.
- Keep painting cheap: cache `QPainterPath`, use `ItemCoordinateCache` for heavy items, avoid full-scene updates.

---

## 13. UI and UX testing

### 13.1 Manual checklist (run at each phase gate that touches the UI, record in `docs/test-log.md`)

| Area | Check | Expected |
|---|---|---|
| Tools | Each shortcut in §4 activates the right tool | tool rail highlights |
| Pen/eraser | Draw, then erase part of a stroke | whole stroke removed for everyone |
| Select | Click, shift-click, marquee, move, resize, rotate | handles correct; others see live movement |
| Layers | Reorder by drag, rename, hide, lock | order syncs; locked object cannot move |
| Properties | Edit X/Y/W/H, fill, stroke, opacity | canvas updates; mixed values show "Mixed" |
| Presence | Two users select/move different objects | colored outlines and name tags appear |
| Undo/redo | Two users edit the same object, then one undoes | toast appears for skipped change |
| Launcher | Open launcher, then start a server and create a room on another machine | board appears without refresh; disappears after server stops |
| Room chat | Send from each role; reconnect; restart server | viewer behavior matches settings; history still there |
| Private messages | A messages B; C watches | only A and B see it; lock header shown; unread badges correct |
| Offline DM | Message a member who is offline; they reconnect | message and unread badge appear |
| Save | `Ctrl+S` as editor, viewer, and with the server unplugged | chip states match §8.3 |
| Recovery | Force-quit the client mid-edit, relaunch | Recover dialog; changes restored once |
| Access flow | Unapproved user, approve, deny, revoke | screens/states match §7; no board or chat before approval |
| Network UI | Pull the cable, reconnect | amber banner, editing continues, board recovers |
| Inspector | Pause/filter/inspect a frame; inspect another user's DM frame | decoded JSON shown, DM text hidden, no UI freeze |
| Export | PNG/JPEG/SVG/PDF/`.lanvas`, each scope | file opens, matches canvas, no cursors in output |
| Open file | Open a signed, a modified, and a password-protected `.lanvas` | correct trust header / password prompt |

### 13.2 Visual regression (optional, if time allows)
Render key states (on a display, or under `xvfb-run`): launcher (empty and with boards), request-access screen, board with 3 remote cursors, chat with a DM open, properties panel, export dialog, recover dialog, inspector. Compare to stored baselines with a small pixel tolerance; update baselines only by owner approval.

### 13.3 Usability test (30 minutes, 2 people outside the team)
Tasks, timed, no help: (1) find a board on the network and get approved, (2) draw a shape and change its color, (3) move an object another user is moving, (4) send a room message and a private message, (5) save and export the board as PDF, (6) find your role. Success: at least 5 of 6 completed unaided. Log every hesitation as a UX bug (S3).

### 13.4 Display, theme, accessibility
- Check 100%, 150%, 200% display scaling on Windows and a HiDPI Linux machine; X11 and Wayland.
- Light and dark theme: no unreadable text, selection, cursor, and private-message colors remain visible.
- Keyboard-only: every panel, the launcher list, and every dialog reachable; focus ring visible.
- Contrast: spot-check text and badges against the ratios in §11.

### 13.5 UI performance
Board with 1000 objects: pan/zoom at 30 fps or better; typing in a text object or chat never lags; the inspector open with a busy stream must not drop canvas frame rate below that (PF-04); exporting the board must not freeze the UI (EX-08).
