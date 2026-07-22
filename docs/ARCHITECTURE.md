# Architecture

## Boundaries

Langhuan 0.1 deliberately has one installable Python package and one configuration file.

1. `reader.py` interprets the Obsidian-specific syntax and emits normalized metadata without absolute source paths.
2. `chunking.py` splits on Markdown headings, preserves code fences and adds document context to every chunk.
3. `index.py` detects changes by content hash and atomically maintains one local index artifact.
4. `search.py` combines dense and lexical ranks with RRF, then optionally applies a local Cross-Encoder.
5. `events.py` records a minimal local audit stream. Remote exporters are separate integrations.

The public reference backend uses a JSON artifact because it is inspectable, dependency-free and sufficient for the demo and small-to-medium vaults. It performs an O(n) scan per query. A vector database adapter should be added only when a reproducible benchmark shows that this ceiling is material.

## Agent contract

Agents call a process boundary rather than importing private Python internals:

```text
langhuan sync
langhuan ask "question" --scope project-name --json
```

JSON results contain a relative source path, heading path, chunk identifier, score and evidence text. The caller decides how much context to place in its prompt and whether a stable conclusion deserves long-term memory.

## Deliberate exclusions

- No autonomous Agent loop: Langhuan supplies evidence, not authority.
- No implicit model download: model preparation and daily inference are different operations.
- No automatic cloud exporter: local retrieval must survive observability outages.
- No plugin framework with one implementation: scope configuration covers current project variation.
- No generated answer in `ask`: answer quality cannot be claimed without choosing and evaluating an LLM.
