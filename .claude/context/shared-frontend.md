<!-- SHARED FRONTEND PRIMER — Patterns shared across all frontend apps -->
<!-- This file is read by all frontend engineers. -->
<!-- Fill this in with YOUR project's shared frontend conventions. -->

## Stack

<!-- List the shared frontend stack. Example: -->
<!-- - React 18 + Vite -->
<!-- - TypeScript strict mode -->
<!-- - React Router 6 / Next.js App Router -->
<!-- - CSS Modules / Tailwind CSS / styled-components -->
<!-- - Zustand for UI state -->
<!-- - TanStack Query for server state -->
<!-- - React Hook Form + Zod for forms -->

## State Management Rules

<!-- Define where each type of state lives. Example: -->
<!-- - Server state: TanStack Query (never duplicated in Zustand) -->
<!-- - Cross-component UI state: Zustand -->
<!-- - Local component state: useState -->
<!-- - Mutations must invalidate relevant queries on success -->

## Client Architecture

<!-- Describe how the frontend talks to the backend. Example: -->
<!-- - REST client for all CRUD operations -->
<!-- - WebSocket client for real-time updates -->
<!-- - REST is the source of truth; real-time signals trigger refetches -->

## Typography

<!-- List your font choices if applicable. -->

## Loading / Error / Empty States

<!-- Define the requirement for handling data states. Example: -->
<!-- - All data-driven views must handle loading, error, and empty states -->
<!-- - Every list, detail, and dashboard panel renders a user-facing fallback for each state -->

## Accessibility Baseline

<!-- List your accessibility requirements. Example: -->
<!-- - WCAG 2.1 AA contrast -->
<!-- - Modals and sheets trap focus; Esc dismisses -->
<!-- - Touch targets minimum 44px / 48px -->
<!-- - Use aria-live for dynamic announcements -->

## Design System / Component Library

<!-- If you use a component library (MUI, shadcn, Radix, etc.), note it here. -->
<!-- If you have a custom design system, describe the token structure. -->
