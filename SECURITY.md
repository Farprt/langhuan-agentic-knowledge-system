# Security Policy

## Reporting

Do not open a public issue containing credentials, private note content or trace payloads. Report a vulnerability through GitHub's private security advisory for this repository. If that channel is unavailable, open a minimal issue asking the maintainer for a private contact method without including the sensitive evidence.

## Data classification

Treat all of the following as sensitive, even when they are generated locally:

- `langhuan.toml`, because it contains local paths and organizational structure;
- `.langhuan/index.json`, because it contains note text, metadata and vectors;
- JSONL events and exported traces;
- model caches and vector database directories;
- API keys, LicenseKeys, DPAPI blobs, cookies and service account files.

These classes are ignored by Git, but `.gitignore` is not a security boundary. Review `git status`, scan the complete Git history and inspect the remote tree before changing a repository to public.

## Runtime defaults

- Hash Embedding runs without a network connection.
- Non-hash embedding and reranking models must resolve to existing local directories.
- Query content is excluded from events unless `observability.include_content = true` is explicitly set.
- The core package contains no AgentLoop, Langfuse or Honcho credentials and performs no remote export.

## Supported versions

Security fixes target the latest released minor version. This alpha release has not yet established a long-term support branch.
