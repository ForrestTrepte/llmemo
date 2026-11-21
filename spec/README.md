# spec/

The language-neutral definition of llmemo's cache format (README requirements X1 and X2).
Every package that reads or writes llmemo caches implements this, and must pass the
vectors.

- [format.md](format.md): the format spec, covering keys, canonical JSON, hashing,
  entries, stored text and vectors, directory layout, atomic writes and locks, reading,
  error codes, and versions.
- [vectors/](vectors/README.md): shared test vectors, as key/hash pairs, complete cache
  trees a writer must produce byte for byte, and damaged caches a reader must reject.
- [tools/](tools/): `reference.py` and `generate_vectors.py` (Python, standard library
  only) generate the vectors. `check_vectors.mjs` is an independent JavaScript
  implementation written from the spec, and it checks them. These are test oracles, not
  the llmemo package, which must not import them.

## Changing the format

Any change to canonicalization, hashing, the entry schema, the stored form, or the
layout must update `format.md` and the vectors in the same commit, and bump the matching
version (format.md §9) if existing files would read differently. To regenerate and check
the vectors, from the repository root:

```
uv run --project python python spec/tools/generate_vectors.py
node spec/tools/check_vectors.mjs
```

The tools use only the standard library, but they run in the package's uv environment so
the Python version is pinned. CI runs both and fails if regenerating changes any file. A
ruff rule stops `spec/tools/` from importing `llmemo`.

The generator reads every case back with the reference reader before writing it. The
JavaScript check must also pass. If the two implementations disagree, the spec is
ambiguous: fix the spec first.
