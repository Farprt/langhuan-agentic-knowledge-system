# Optional integrations

Integrations are adapters around the local core, not prerequisites.

| Directory | Purpose | Included here |
|---|---|---|
| `honcho/` | stable cross-session memory | boundary and write policy |
| `loongsuite-agentloop/` | Trace export for Agent/RAG analysis | redacted environment contract |
| `langfuse/` | alternative self-hosted/cloud observability | redacted environment contract |

The repository intentionally does not copy Honcho or Langfuse source, install a commercial probe, start containers, or enable cloud upload. Follow upstream installation documentation, pin a reviewed version, and keep credentials outside Git.

All exporters should consume local events after the RAG operation succeeds. An exporter failure must not turn a successful local retrieval into an Agent failure.
