## Scope

`apps/web/` — the public-facing SPA at http://localhost:5173. Read `shared-frontend.md` first; this file is the web-app overlay.

## Routes

| Route | Page | Notes |
|---|---|---|
| `/` | Workspace picker / redirect | Redirects to last-used workspace's boards |
| `/w/:slug/boards` | Board list for a workspace | Workspace resolved by slug |
| `/boards/:id` | Kanban board | The core screen; Socket.IO room `board:{boardId}` |
| `/tasks/:id` | Task detail | Rendered as a route-driven dialog over the board when navigated from it |
| `/settings` | Account + workspace settings | Members, plan/billing (Stripe portal link) |

All routes are auth-gated via Clerk (`<SignedIn>` boundary); unauthenticated users hit Clerk's sign-in.

## Layout Shell

- Left sidebar: workspace switcher (top), board nav for the active workspace, settings link (bottom). Collapse state in `ui.store.ts`.
- Topbar: breadcrumb (workspace / board), search, presence avatars, user menu (Clerk `<UserButton>`).
- Content area renders the route. Sidebar + topbar persist across navigation.

## Kanban Interaction Rules

- Columns are the four task statuses: TODO, IN_PROGRESS, IN_REVIEW, DONE. Fixed set — no custom columns in v1.
- Drag a card between columns -> `PATCH /api/tasks/:taskId` with new `status` + `position`. Drag within a column -> `position` only.
- `position` is an integer; the client computes the new value from neighbors and the server accepts it as-is.
- While dragging: card gets a ghost style, target column highlights, drop is cancelled with Escape.
- Keyboard path: focus card, Space/Enter picks up, arrows move, Enter drops (see a11y baseline in `shared-frontend.md`).

## Time-Tracking Widget

- Each task card and the task detail show a start/stop toggle -> `POST /api/tasks/:taskId/time-entries`.
- Only one running entry per user; starting a timer while another runs stops the old one first (server enforces, client reflects).
- A running timer is surfaced globally in the topbar with elapsed time ticking client-side from `started_at`; on stop, the server's `duration_seconds` is authoritative.

## Optimistic Updates + Socket.IO Reconciliation

- Mutations (move, create, edit, delete task) apply optimistically via TanStack Query `onMutate`, roll back `onError`.
- On board mount, join `board:{boardId}`; on workspace mount, join `workspace:{workspaceId}`.
- Incoming events (`task.moved`, `task.created`, `task.updated`, `task.deleted`) update the Query cache directly via `setQueryData`.
- Echo suppression: events carry the originating client's socket id; the sender ignores its own echoes so optimistic state isn't double-applied.
- REST is the source of truth: on reconnect after a socket drop, invalidate `['tasks', boardId]` and refetch rather than trusting replayed events.
