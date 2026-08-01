# Architecture

## Boundaries

Langhuan 0.1 deliberately has one installable Python package and one configuration file.

1. `reader.py` interprets the Obsidian-specific syntax and emits normalized metadata without absolute source paths.
2. `catalog.py` maintains a body-free structural inventory, collection registry and optional reading-ledger checks.
3. `chunking.py` splits on Markdown headings, preserves code fences and adds document context to every chunk.
4. `index.py` detects changes by content hash and atomically maintains one local index artifact.
5. `search.py` combines dense and lexical ranks with RRF, then optionally applies a local Cross-Encoder.
6. `events.py` records a minimal local audit stream. Remote exporters are separate integrations.

The public reference backend uses a JSON artifact because it is inspectable, dependency-free and sufficient for the demo and small-to-medium vaults. It performs an O(n) scan per query. A vector database adapter should be added only when a reproducible benchmark shows that this ceiling is material.

## Agent contract

Agents call a process boundary rather than importing private Python internals:

```text
langhuan catalog envelope --path "Sources/Books/Example.md" --workflow auto --action update --compact
langhuan catalog find "existing concept" --collection concepts
langhuan catalog context --query "known title or alias" --compact
langhuan sync
langhuan ask "question" --scope project-name --json
```

JSON results contain a relative source path, heading path, chunk identifier, score and evidence text. The caller decides how much context to place in its prompt and whether a stable conclusion deserves long-term memory.

Catalog commands also use JSON, but never return note bodies. A Task Envelope contains
the exact target, lifecycle workflow, action, processor, bounded entrypoints, matching
Reading Ledger units, mandatory checks, mechanical blocks, link counts and independent
watermarks. This is deterministic task routing; semantic relevance remains the
responsibility of `ask`. `context --query` is deterministic lexical discovery only;
zero matches return a compact failure contract that directs the caller to semantic RAG.
Registry-wide navigation requires `context --global`, and a complete filename inventory
requires the explicit `catalog list --all` command.
All catalog commands accept `--compact`; agent launchers should use it to avoid
spending tokens on pretty-print whitespace.
Find responses include exact total/truncation metadata. Envelope/context responses carry
the structural catalog revision, the catalog content digest, and a byte-exact
reading-ledger revision.

Every agent follows the same process contract:

1. Regenerate a bounded Task Envelope for the target at task start, after compaction,
   or after the target, workflow, action, or scope changes. Use `status` plus `find` or
   diagnostic `context` only when no target is known.
2. Execute every required check. Treat processors and related collections as routing
   priorities rather than completeness whitelists: extract candidates from the actual
   content, resolve exact identities and graph signals, then use lexical/semantic
   retrieval when cross-domain discovery is required.
3. Ensure a stable ID before creating, copying, moving, renaming, merging, splitting,
   or deleting a durable note.
4. Give concurrent agents non-overlapping file ownership.
5. After writes, refresh Catalog, refresh the RAG inputs that changed, and run the
   repository's unified machine-readable verification command.

Semantic Agent behavior is evaluated separately from structural correctness. The
`catalog evaluate-agent` command consumes explicit case definitions and evidence-only
submissions. Deterministic Envelope, lookup, and context expectations are executed
against the live Catalog; Agent cases are scored by required paths found and opened,
query evidence, forbidden adopted relations, ambiguous-target safety, and pairwise
agreement. It deliberately does not use an LLM judge or claim open-world completeness.

## Catalog consistency

The catalog scans its own include/exclude boundary rather than inheriting the RAG
boundary, so Inbox and System can remain structurally visible without becoming retrieval
content. Markdown files contribute selected frontmatter and structural links.
Non-Markdown files are excluded by default; when explicitly enabled they contribute
filesystem metadata only and are never opened for body extraction or hashing.
Symbolic links, junctions and other path aliases are skipped so they cannot escape the
vault or bypass a configured include/exclude boundary. Cache and OS metadata files are
also ignored.

Every catalog query performs an incremental stat scan. Changed Markdown files are parsed
and hashed; `validate` verifies every Markdown hash. Writers are serialized with a
native `msvcrt.locking`/`fcntl.flock` lock, then publish a unique same-directory
temporary file with `fsync` and `os.replace`. Readers therefore observe either the old
complete JSON document or the new complete document. Short Windows sharing violations
during replacement are retried within a fixed two-second bound.

Collection registry paths may overlap. Files retain every matching collection and choose
the most specific path as primary. This lets a general `sources` collection coexist with
a `history_sources` collection whose related routes include concepts, events, people,
and time.

Catalog is a rebuildable structural projection, not another manually maintained
knowledge store and not a separate graph database. Version 2 exposes four distinct
watermarks instead of pretending every component shares one global revision:

- `catalog_revision` hashes a canonical structural projection: schema and scope,
  parser/projection versions, stable or legacy identity, current path, classification,
  selected metadata, aliases, collections, links and embeds. It excludes body hashes,
  mtimes, file sizes, generation timestamps and validation issues.
- `content_digest` hashes the canonical ordered set of Markdown
  `{stable-id | legacy-path, raw-sha256}` pairs. Moving an ID-bearing note does not
  change this digest; moving a legacy note does.
- `reading_ledger_revision` is the complete SHA-256 of one exact UTF-8 byte snapshot.
  Schema validity is reported separately.
- `rag_input_digest` hashes the exact retrieval boundary and file hashes together with
  parser, chunker and tokenizer versions, chunk parameters, embedding model identity
  and resolved local model snapshot.

RAG reports `storage_consistent`, `fresh`, and `ready` separately. Storage consistency
only compares state/lexical/vector chunk identity; freshness compares indexed and
current input digests; readiness requires both plus the current pipeline contract.

Stable identity is deliberately gradual. Durable paths configured by
`catalog.identity_paths` use frontmatter IDs shaped as `note_<uuid4-hex>`. Existing
files without IDs remain discoverable through explicit `legacy-path:<relative-path>`
identities, but an ID must be assigned before a lifecycle or path operation. Copying a
note requires a new ID. Duplicate or malformed IDs fail validation. The Reading Ledger
may add `outputs[].extensions.note_id` without a schema bump; ID is authoritative when
present while path remains a human-readable locator.

The optional reading ledger is versioned JSON stored inside the vault. It records
raw/draft/official path relationships and lifecycle status; it does not replace Markdown
as the source of truth. Cleanup requires an integrated official output plus hashed raw
and draft evidence; removed inputs remain expressible as tombstones.

## Deliberate exclusions

- No autonomous Agent loop: Langhuan supplies evidence, not authority.
- No implicit model download: model preparation and daily inference are different operations.
- No automatic cloud exporter: local retrieval must survive observability outages.
- No plugin framework with one implementation: scope configuration covers current project variation.
- No generated answer in `ask`: answer quality cannot be claimed without choosing and evaluating an LLM.
- No daemon or filesystem watcher: a cheap stat scan keeps the catalog current at the present scale.
- No automatic entity extraction: agents extract candidate names, then use exact catalog lookup or semantic RAG.
