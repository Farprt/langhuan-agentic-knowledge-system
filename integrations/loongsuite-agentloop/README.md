# AgentLoop / LoongSuite trace boundary

The intended exporter maps one local Langhuan event to a Trace span after the local operation succeeds. Default attributes should contain only operational metadata:

- command and status;
- duration and result count;
- normalized embedding backend kind, never its local path;
- scope name;
- query length, not query text or a reusable query fingerprint;
- no note body, retrieved chunk or absolute path.

Vendor-specific values such as LicenseKey, region, workspace, project and OTLP endpoint must be provided by environment variables or an operating-system secret store. Never commit values copied from the cloud console.

`.env.example` documents names only. It is not consumed by the 0.1 core and does not enable upload. No AgentLoop exporter is shipped in 0.1.
