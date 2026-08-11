# AgentLoop / LoongSuite export boundary

Langhuan 0.2 can explicitly map one completed local event to a remote Trace span. This exporter is outside the retrieval path: local indexing and retrieval finish before any upload is attempted. Default output contains only operational metadata:

- task, run and step correlation IDs;
- Agent name and a hashed session key;
- component, operation, status and duration;
- reviewed scalar fields such as result count, token estimate, entrypoint count/size and required-check count;
- no query text, note body, retrieved chunk or absolute path.

The exact scalar allowlist is defined by `SAFE_ATTRIBUTE_KEYS` in `src/langhuan/observability_export.py`. A relative target path is added only when the user explicitly passes `--include-local-context` for a reviewed debugging case.

Vendor-specific values such as LicenseKey, workspace, project and OTLP endpoint must be provided as environment variables. A separate secret manager may inject those variables before launch; Langhuan itself does not read an operating-system secret store. Never commit values copied from the cloud console.

`.env.example` documents variable names only and does not enable upload. Preview is the default and needs no credentials:

```powershell
langhuan trace export --provider agentloop
```

Sending is an explicit action and requires the optional dependencies plus the environment variables listed in `.env.example`:

```powershell
python -m pip install -e ".[observability]"
langhuan trace export --provider agentloop --send
```

The repository provides only this event exporter. It does not bundle LoongSuite Pilot, install a vendor probe, start a background collector or redistribute provider source code.
