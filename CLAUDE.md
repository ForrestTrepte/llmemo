# CLAUDE.md

Instructions for AI agents working in this repo. The project's goals, requirements,
decisions, and next steps are in the README. Read it first:

@README.md

Keep this file to agent workflow only. If information would also help a human
contributor, put it in README.md or a linked doc in `docs/`. Don't rely on private agent
memory to carry lessons between sessions. If something is worth remembering, write it
into a checked-in file.

## Starting a session

- Read the README's **Decisions**, **Open questions**, and **Next steps** first. Don't
  reopen a recorded decision unless the user asks to. If you think one is wrong, say so
  and ask.
- If the user's request is vague, propose the top item in **Next steps**.

## Ending a session (or finishing a meaningful chunk of work)

- Update the README: add new decisions to the Decisions table (with the date and
  reason), remove open questions that have been answered, and rewrite **Next steps** so
  a fresh session can continue without this conversation's context.
- Leave history to git. Don't keep a changelog in the docs.
- Commit only when the user asks.

## Requirements

- Requirement IDs (K1, S5, …) are stable. Don't renumber them. To withdraw one, strike it
  through and say why. Add new requirements at the end of their group.
- When adding a requirement, tag where it came from (`[CE n]`, `[T]`, `[WS]`, `[SC]`,
  `[user]`).
- Refer to requirement IDs in code comments, tests, and the spec when they explain why
  something is done.
- ConceptExpansion's list in its `docs/simulated-playtesting.md` is the first user's
  checklist. If you change how one of those requirements is met, say so to the user.
  Don't edit the other repo.

## The format is a contract between languages

- Any change to how keys are canonicalized or hashed, to the entry schema, or to the
  directory layout must update the spec and the test vectors in `spec/` at the same time,
  and bump the matching format version if old files would read differently.
- Never let Python-specific behavior into the format: `str()` or `repr()` of objects,
  `hash()`, pickle, `json.dumps` defaults, `Decimal`, enum names that nobody chose on
  purpose, or local-time datetimes. Before relying on any serialization detail, check it
  against [docs/cross-language.md](docs/cross-language.md).
- Even before a TypeScript package exists, design APIs and files so it could be written
  from the spec alone.

## The other projects

- SoCloverAI, Threshold, WordSpace, and ConceptExpansion are reference material for
  now. **Don't change those repos** (no commits, branches, PRs, or issues) unless the user
  asks.
- To read one, make a shallow clone into the scratchpad:
  `gh repo clone ForrestTrepte/<repo> <scratchpad>/repos/<repo> -- --depth 1`. Their caches
  are large (WordSpace about 104k files, SoCloverAI's embeddings 2.1 GB), so exclude
  cache folders when listing or searching. For example:
  `git ls-files | grep -v -E 'cache/|llm-cache/|embeddings-cache/'`.
- Update [docs/prior-art.md](docs/prior-art.md) when you learn something new about those
  caches. Keep its "as of" date current.

## Tests and spending

- Tests never call a paid API. Use fake callables, and the memory store or a temp
  directory.
- Never put API keys or other secrets in code, tests, test vectors, or docs.

## Asking the user

- Ask about things that are the user's call: scope, public API names, the license,
  publishing, and anything in **Open questions**. For routine choices, pick a sensible
  default, record it, and say what you chose.
