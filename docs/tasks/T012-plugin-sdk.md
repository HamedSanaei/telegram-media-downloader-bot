# T012 - External extractor plugin scaffold

**Status:** complete (2026-07-23)

`plugins/example_extractor/` is an independent `yt_dlp_plugins.extractor` distribution with Python
and yt-dlp compatibility metadata, a functional operator-owned JSON endpoint example, unit and
opt-in contract tests, and lockfile/Docker installation instructions. Castbox remains intentionally
unimplemented until its public extraction path is confirmed.

## Deliverables

- Create a separate package template for `yt_dlp_plugins.extractor` implementations.
- Keep the plugin outside the application package and upstream source tree.
- Add compatibility metadata and plugin contract tests.
- Document installation through the lockfile and Docker build.
- Use the template for Castbox only after confirming a maintainable public extraction path.
