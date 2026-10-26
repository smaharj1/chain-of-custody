<!-- DOMAIN GLOSSARY TEMPLATE (CO-30). Copy to docs/domain-glossary.md in YOUR project; it does not belong in the kit's docs. -->
<!-- Why: ambiguous domain words ("account", "member", "booking", "active") were among the most common brief gaps in real runs. -->
<!-- The PM and Architect read this before writing requirements and designs. Engineers read it when a brief uses a term. -->
<!-- Keep it to one line per term. If a definition needs a paragraph, it belongs in the design, not here. -->

# Domain Glossary: <Project Name>

One line per term. Say what it means **in this product**, name the table or type that holds it, and
say what it is **not** if two words are easily confused.

| Term | Meaning in this product | Where it lives in code | Not to be confused with |
|---|---|---|---|
| <!-- e.g., Workspace --> | <!-- a team's private area; owns projects and members --> | <!-- `workspaces` table, `Workspace` type --> | <!-- "Organization" (billing entity, one per paying customer) --> |
| <!-- e.g., Member --> | <!-- a user who belongs to a workspace, with a role --> | <!-- `memberships` table --> | <!-- "User" (a login; may belong to several workspaces) --> |

## Status words

<!-- Every status or state word, with its allowed values and what triggers each move. Example: -->
<!-- - Task status: `todo` -> `doing` -> `done` (any -> `archived`). `done` is set only by the assignee. -->

## Words we do not use

<!-- Synonyms the team has decided against, so the agents do not drift. Example: -->
<!-- - Say "workspace", never "team" or "org" in code, UI copy or API paths. -->
