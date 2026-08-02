# ADR 0002 — The legal line is a revision parameter bound only into `copy.pack`

Date: 2026-08-02
Status: Accepted

## Context

PRD §4.2 requires that changing `zero sugar` to `no added sugar` invalidates
**exactly four** nodes — `copy.pack`, `audio.narration`, `graphic.end_card`,
`compose.delivery_package` — leaving the other fourteen reusable. AS-01 restates
this as a final acceptance scenario and §22.2 as a mandatory test.

The PRD does not say where that phrase lives, and the choice decides whether the
requirement is satisfiable at all.

`source.brief` (node 1) is a plausible home: it is the creative brief, and the
legal line is part of the creative direction. But `plan.shots` (node 4) depends on
`source.brief`. Changing the brief would therefore change `plan.shots`, which every
keyframe consumes, which every clip consumes, which the delivery package consumes.
The cascade reaches all 18 nodes and the product's central demo shows 18 rebuilds
instead of 4 — the opposite of its claim.

## Decision

The legal phrase is a **project-revision parameter** (`legal_line`), bound into
node operations through the template's allow-listed `parameter_bindings` (PRD §12.1
step 1: "Resolve template parameters from the revision through an allow-listed
mapping"). Exactly one node binds it:

```python
TemplateNode(
    stable_key="copy.pack",
    parameter_bindings=(
        ParameterBinding(operation_key="required_legal_phrase", parameter="legal_line"),
    ),
    ...
)
```

`source.brief` separately binds `brief_text`. The two parameters are independent,
so editing the legal line does not touch the brief's content hash.

The compiler enforces the allow-list in both directions: a node binding an
undefined parameter is a compile error, and a parameter no node binds cannot reach
any operation. `test_unbound_parameters_cannot_reach_the_operation` asserts
`required_legal_phrase` appears in no compiled node other than `copy.pack`.

## Consequences

- The invalidation set follows from the graph rather than being special-cased:
  `copy.pack` -> `audio.narration`, `graphic.end_card`, `compose.delivery_package`.
  Four nodes, fourteen reused, derived by the impact algorithm.
- `image.poster` survives, which is the more interesting half. It descends from
  `image.keyframe.01`, so a naive "invalidate everything downstream of anything
  that changed" would drop it. §4.2 explicitly requires the poster to remain
  reusable.
- Parameter bindings are the general blast-radius control, not a one-off. Any
  future editable field gets scoped the same way, and its invalidation set is a
  property of where it binds.
- The allow-list is a security boundary too: nothing else in a project revision can
  reach a node operation, so a revision field cannot smuggle content into a prompt.

## Alternatives rejected

**Put the legal line in `source.brief`.** Invalidates all 18 nodes. Fails AS-01.

**Add a dedicated `source.legal_line` SOURCE_TEXT node.** Works, and keeps the
phrase content-addressed like other sources — but it makes the seed graph 19 nodes,
and §4.2 specifies exactly 18 with a fixed dependency table.

**Special-case the four keys in the impact algorithm.** Would produce the right
demo and a worthless product. The impact algorithm must derive the set, or the
system's central claim is a hardcoded string.
