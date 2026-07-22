# Langfuse observability boundary

Langfuse is an alternative consumer of the same local event stream. The recommended order is:

1. complete the local index or retrieval operation;
2. append the local JSONL event;
3. sanitize and preview a pending export batch;
4. upload explicitly;
5. checkpoint only events acknowledged by the server.

This preserves a local source of truth and permits replay after an outage. Query and retrieved content should remain disabled until the user has a concrete evaluation question that cannot be answered from metadata alone.

Use the official Langfuse packages or container images. This repository does not copy its source or enterprise-licensed directories. `.env.example` is a placeholder contract and does not start a service.
