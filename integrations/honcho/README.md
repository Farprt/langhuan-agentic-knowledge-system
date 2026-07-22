# Honcho memory boundary

Honcho is suitable for stable cross-session conclusions such as a confirmed preference, durable project decision or reusable failure lesson. It is not a duplicate store for every retrieved chunk or complete conversation.

Recommended flow:

1. Retrieve project evidence with `langhuan ask ... --json`.
2. Let the Agent complete and validate the task.
3. Write only the concise, stable conclusion to Honcho.
4. On the next task, combine relevant Honcho memory with fresh Langhuan evidence.

Use the upstream Honcho deployment rather than copying its source into this repository. Keep its API key, workspace identifier, database and embedding service configuration outside Git. A memory outage must degrade to “no cross-session memory,” not “no local RAG.”

No Honcho client is shipped in 0.1 because the private deployment contract has not yet been converted into a provider-neutral, reproducible test fixture.
