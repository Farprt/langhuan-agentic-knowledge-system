# Langhuan Showcase

Zero-dependency, Chinese-first project overview for first-time visitors and recruiters. It is a presentation layer over the repository documentation, not a second source of truth.

## Run locally

```powershell
cd showcase
python -m http.server 8000
```

Open `http://localhost:8000/`.

## Verify

The repository's standard-library test suite checks the HTML, JavaScript, stylesheet and social card. No build step, package manager, database, cloud account, private vault or telemetry credential is required.

## Content rules

- Markdown and repository tests remain the source of truth.
- Unmeasured performance figures stay marked as pending.
- Private notes, real logs, local paths, tenant identifiers, and credentials never enter the site.
- Claims about the public engine must match the current tagged code, not only the private production case.

The directory can be served unchanged by GitHub Pages or any static file host. Publishing the site is a separate explicit action.
