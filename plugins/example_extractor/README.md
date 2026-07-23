# External extractor plugin template

This is a separate Python distribution using yt-dlp's `yt_dlp_plugins.extractor` namespace. Rename
the distribution, module, class, `IE_NAME`, and `_VALID_URL` before implementing a real public
extractor. Do not place plugin code in the bot application or modify the upstream yt-dlp package.

The included extractor is a functional JSON-manifest example for operator-owned test infrastructure:
`https://media.example.org/api/items/<id>` must return `id`, `title`, and a public `media_url`.

Install through the root lockfile only after review:

```bash
uv add ./plugins/example_extractor
uv lock
uv sync --frozen --group dev
```

The root Dockerfile copies `plugins/` before `uv sync`, so the locked path dependency is reproducible
inside the image. Castbox is intentionally not implemented until a maintainable public extraction
path and permission to support it are confirmed.
