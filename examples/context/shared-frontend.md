## Stack

- React 18 + Vite, TypeScript strict mode.
- React Router 6 (route objects, not JSX routes).
- Applies to both `apps/web/` and `apps/admin/`. App-specific rules live in `app.md` / `admin-app.md`.

## State Management

- **TanStack Query owns ALL server state.** Query keys are array-form and hierarchical: `['workspaces']`, `['boards', workspaceId]`, `['tasks', boardId]`.
- **Zustand is for UI-only state**: sidebar collapsed, active dialog, drag state, filter selections.
- Rule: server state is NEVER duplicated into Zustand. If it came from the API, it lives in the Query cache and nowhere else.
- Mutations invalidate the narrowest matching query key. Optimistic updates go through TanStack Query's `onMutate`/`onError` rollback, not manual cache pokes.

## Forms

- React Hook Form + Zod via `zodResolver`.
- Zod schemas are imported from `@taskflow/db` validators — never redefined in the frontend.
- Submit buttons disable while `isSubmitting`; server-side `ValidationError` details map onto field errors via `setError`.

## Styling and Components

- Tailwind CSS + shadcn/ui. shadcn components live in `components/ui/` and are not hand-edited beyond theme tokens.
- Composite components build on shadcn primitives; no ad-hoc CSS files, no inline `style` except for computed positions (drag ghosts).
- Icons: lucide-react only.

## Accessibility Baseline

- Every input has a `<label>` (or `aria-label` for icon-only controls).
- Dialogs and popovers trap focus and close on Escape (shadcn/Radix gives this for free — don't bypass it with custom modals).
- Kanban drag-and-drop must be keyboard-operable: cards focusable, moved with arrow keys + Enter via the drag library's keyboard sensor.
- Interactive elements are real `<button>`/`<a>`, never clickable `<div>`s.

## API Client Pattern

- Single typed fetch wrapper in `lib/api-client.ts`: attaches the Clerk session token, sets JSON headers, prefixes `/api`.
- Error envelope is uniform: `{ "error": "ErrorClassName", "detail": "message" }`. The wrapper throws a typed `ApiError { status, error, detail }`; components never parse raw responses.
- Request/response types are imported from the shared Zod schemas — no hand-written duplicate interfaces.
- All data access goes through hooks in `hooks/` (e.g. `useTasks(boardId)`); components never call the wrapper directly.

## File Naming

- Components: PascalCase — `TaskCard.tsx`, `BoardColumn.tsx`.
- Hooks: camelCase with `use` prefix — `useTasks.ts`, `useMoveTask.ts`.
- Zustand stores: `<name>.store.ts` — `ui.store.ts`, `dragState.store.ts`.
- Non-component modules: kebab-case — `api-client.ts`, `format-duration.ts`.
- Route pages live in `pages/`, shared building blocks in `components/`, hooks in `hooks/`.
