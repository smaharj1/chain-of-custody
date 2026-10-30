# loop.config key registry

Every `loop.config` key is declared here, one JSON file per phase. The loader
(`kitlib/registry.py`) merges every `schema/*.json` (`v1.json` first, then the rest
alphabetically) and refuses duplicate key names and duplicate section ids.
`.claude/loop.config.md` and `examples/loop.config.md` are **generated** from the registry by
`python3 tools/gen-config.py --write`; a test fails if they drift. Never edit them by hand.

To add a key: add it to your phase's file (create `schema/<phase>.json` if you have none),
run `python3 tools/gen-config.py --write`, commit both.

## File shape

```json
{
  "schema_version": 1,
  "phase": "3-pr-delivery",
  "sections": [
    {"id": "delivery", "title": "Delivery", "comment": ["Optional lines under the heading."]}
  ],
  "keys": [
    {
      "name": "delivery",
      "type": "enum",
      "choices": ["local", "pr"],
      "default": "local",
      "owner_phase": "3-pr-delivery",
      "h6_protected": true,
      "evidence_gated": false,
      "deprecated_by": null,
      "section": "delivery",
      "doc": ["How finished work leaves the machine.", "  local = merge to local main, never push"]
    }
  ]
}
```

## Key fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Lower-case snake case. Dots nest: `gates.g6` is `g6:` under `gates:`. A final `.*` (`gates.*`) declares a family and is not rendered. |
| `type` | yes | `string`, `command` (a string; `<command>` is the "not filled in yet" placeholder), `int`, `number`, `bool`, `enum` (needs `choices`), `list`, `map` |
| `default` | yes | Must satisfy `type`. Used when the key is absent, and rendered into the generated config. |
| `owner_phase` | yes | The phase that owns the key (free text, for tracing). |
| `h6_protected` | yes | `true` if changing it is a guardrail change (report §6.1 H6). A diff that touches it routes to a human. |
| `evidence_gated` | yes | `false`; `true` (any non-default value needs the evidence gate); or a list of values that need it (`["worktree"]`). The doctor refuses those values until `docs/baseline/evidence.json` is complete (IMP-1). |
| `doc` | yes | Comment text. A string (split on newlines) or a list of lines. The first line goes after the value on the same line; the rest align beneath it. A first line of `""` starts the comment on the next line. |
| `deprecated_by` | yes | `null`, or the name of the key that replaces this one. The old key still loads (its value moves to the new key, with a warning). No chains. |
| `section` | yes for rendered keys | A section id from any schema file. Keys render grouped by section, in registry order. |
| `choices` | enum only | Allowed values. |
| `after_doc` | no | Comment lines rendered at column 0 after the key. |
| `example` | no | `true` to include the key in `examples/loop.config.md` as-is, or `{"value", "doc", "after_doc"}` to override. Keys with no `example` do not appear in the example file. |

## Sections

`{"id", "title", "comment", "example_title", "example_comment"}`. `title` renders as
`# --- title ---` followed by the comment lines. The `example_*` fields apply to the example file
and default to the non-example values.

## Templates

`templates/header.md` and `templates/notes.md` are the hand-written text before and after the yaml
block of `.claude/loop.config.md`; `header.example.md` and `notes.example.md` do the same for the
example file. `header*.md` must end with the opening ` ```yaml ` line.

## Rules the loader enforces

- no duplicate key names or section ids across files
- a key's `default` and `example.value` satisfy its `type`
- `deprecated_by` points at an existing key, with no chains
- no key sits underneath another leaf key
- unknown fields are rejected, so a typo cannot silently do nothing

Validation of a user's config against the registry (`kitlib/config.py`) reports unknown keys with
a "did you mean" suggestion and type errors with the line number.
