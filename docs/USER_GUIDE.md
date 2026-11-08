# LANvas user guide

## Start

1. **One machine runs the server:** `lanvas-server` (or `python -m server.main`). It prints the LAN addresses to use.
2. **Everyone runs the client:** `lanvas-client` (or `python -m client.main`). Enter your name.
3. **Boards on the network appear by themselves.** Pick one and press **Join**.

If nothing appears after a few seconds, check that you share a network and that the firewall allows UDP 47800, or use **Join by address**.

## Create a room

Choose **Create room…**, then name it and pick the server. You become the owner, and the only member.

The room code is shown in **Share** while you stay connected. The proof-of-concept server stores room codes as plain text, so do not reuse a sensitive password as a room code.

## Getting access

Being on the same network is not automatic access. A visitor sees a **Request access** screen with their client ID, and nothing from the board. The ID is a convenience for this proof of concept, not secure authentication; another client can copy it.

The owner gets a notice with **Review**, then chooses **Approve as Editor**, **Approve as Viewer**, **Deny**, or **Block this device**. Compare client IDs if helpful. Approved members are recognized automatically when they return with the same saved ID.

| Role | Can do |
|---|---|
| Owner | Everything, including approvals, roles, restoring versions |
| Editor | Draw and edit, chat, Ctrl+S |
| Viewer | Watch and chat; export only if the owner allows it |

## Tools

| Key | Tool | Key | Tool |
|---|---|---|---|
| `V` | Select, move, resize, rotate | `P` | Pen |
| `F` | Frame | `E` | Eraser (removes whole strokes) |
| `R` | Rectangle | `T` | Text |
| `O` | Ellipse | `S` | Sticky note |
| `L` / `Shift+L` | Line / arrow | `S` | Sticky note |

Hold `Space` to pan. `Ctrl`+scroll zooms; `Ctrl+0` is 100%, `Shift+1` fits the board. `Ctrl+D` duplicates, `Ctrl+G` groups, `Del` deletes, `Ctrl+]` and `Ctrl+[` change stacking order. `Ctrl+\` hides the panels.

## Undo

`Ctrl+Z` and `Ctrl+Shift+Z` affect only your own changes. If someone has since changed the same property, that property is left alone and a notice says so.

## Chat

The **Chat** tab holds the room channel and your private conversations. Start one with `Ctrl+Shift+M`, from **Message** in the People list, or by clicking a name in chat.

Private messages go only to the two people in them. They are not end-to-end encrypted: whoever controls the server machine can read them on its disk, and they cross the network in plaintext.

History stays through reconnects and server restarts. Offline members get their messages, with an unread badge, when they return.

## Saving

**Ctrl+S** makes a verified checkpoint on the server. The status bar shows the result. If the server is unreachable, the checkpoint request is sent when the connection returns. Crash recovery keeps unsent changes in the local recovery journal.

The owner can restore any saved version under **Version history**. Restoring affects everyone, and `Ctrl+Z` undoes it.

## Export

**Ctrl+Shift+E** exports PNG, JPEG, SVG, or PDF, for the whole board, the selection, or one frame. PDF can put each frame on its own page.

## When things go wrong

- **Connection lost:** keep working. Your changes and messages are sent when the connection returns.
- **The app crashed:** the launcher shows a banner. **Recover** rejoins and sends your unsaved changes, without creating duplicates.
- **The server crashed:** restart it. It repairs itself on startup and prints a line per room. Boards, chat, and private messages come back.

## The Packet Inspector

`Ctrl+Shift+I` shows every frame with its sequence number, size, and round-trip time. The **Chaos** tab adds delay and duplicates and drops the connection, to show that the board stays consistent. It affects only your own client.

`Ctrl+Shift+H` shows the board checksum. When every client shows the same checksum, every client has the same board state.
