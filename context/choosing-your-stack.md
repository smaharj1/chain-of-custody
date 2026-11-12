# Choosing Your Stack

Your "tech stack" is the set of tools your project is built with. The agents need to know it (it lives in `.claude/CLAUDE.md`). If you already know what you're using, just fill it in. If you **don't know what to pick**, this guide gives you safe, popular defaults by project type.

> **You usually don't pick this alone.** When you start a new idea with `/planner`, it asks about your stack during its interview, recommends sensible choices if you're unsure, and records them into `.claude/CLAUDE.md` for you, so you rarely fill that file in by hand. You can also ask any time: *"I'm building `<describe your idea>`. I'm not technical — recommend a stack and explain each choice simply."* This document is the reference behind those recommendations.

None of these are wrong answers. They're all mainstream, well-supported choices that Claude knows well. Pick the row that matches your project and copy it into `CLAUDE.md`.

---

## If you're not sure at all: the safe default

For most web apps, this combination is popular, well-documented, and works well with AI assistance:

| Layer | Recommendation | Why |
|---|---|---|
| Frontend | **Next.js (React) + TypeScript** | The most common choice; handles both pages and simple backend logic |
| Styling | **Tailwind CSS** | Fast to build with; huge community |
| Database | **PostgreSQL** | Reliable, free, handles almost any need |
| ORM | **Prisma** | Beginner-friendly way for code to talk to the database |
| Auth | **Clerk** or **Supabase Auth** | Handles login/signup so you don't build it yourself |
| Hosting | **Vercel** | Deploys Next.js with almost no configuration |
| Payments (if needed) | **Stripe** | The standard for taking payments |

If you have no other information, start here. You can change individual pieces later.

---

## By project type

### A simple website or blog
- Frontend: **Astro** or **Next.js**
- Styling: **Tailwind CSS**
- Database: often **none needed** (or a headless CMS like Sanity)
- Hosting: **Vercel** or **Netlify**

### A SaaS web app (users log in, data is saved)
- Use the **safe default** above. This is what it's designed for.

### A mobile app
- Frontend: **React Native (Expo)** — write once, runs on iPhone and Android
- Backend/Database/Auth: **Supabase** (database + auth + storage in one)
- Note: this kit's frontend agents are oriented to web, but the patterns transfer; adjust the `app.md` primer for React Native.

### An internal tool / dashboard
- Use the **safe default**, and lean on a component library like **shadcn/ui** or **MUI** for ready-made tables, forms, and charts.

### An API or backend service (no user interface)
- Backend: **Node + Express + TypeScript**, or **Python + FastAPI**
- Database: **PostgreSQL** + **Prisma** (Node) or **SQLAlchemy** (Python)
- Delete the frontend agents (`app-engineer`, `admin-app-engineer`) — you won't need them.

---

## What each layer means (quick version)

- **Frontend** — what users see in the browser. Pick a framework: Next.js, React, Vue.
- **Styling** — how it looks. Tailwind CSS is the common default.
- **Backend** — the server logic. With Next.js you may not need a separate one at first.
- **Database** — where data is stored. PostgreSQL is a safe default.
- **ORM** — the bridge between your code and the database. Prisma or Drizzle.
- **Auth** — login and accounts. Clerk, Supabase Auth, and Auth0 save you huge effort.
- **Hosting** — where it runs live. Vercel (for Next.js) is the easy path.
- **Payments** — Stripe if you charge money.
- **IaC** — only relevant if you manage your own cloud servers. Most beginners can skip this and use a managed host (Vercel, Supabase). Delete the infra agent if so.

See [GLOSSARY.md](../GLOSSARY.md) for fuller definitions.

---

## A note on "managed" services

As a non-technical builder, prefer **managed services** — ones that handle the hard parts for you:

- **Vercel / Netlify** host your app without server management.
- **Supabase / Clerk / Auth0** handle accounts and login.
- **Stripe** handles payments and compliance.

They cost a little money at scale, but they save a lot of time and remove whole categories of security mistakes. They also mean you probably don't need the `infra-engineer` agent at all, so feel free to delete it.
