# Configuration reference

The application reads one local YAML file. The default path is `config.yaml`; operators may select
another path with `APP_CONFIG_PATH`, but secrets themselves remain in YAML.

## Rules

- Start from `config.example.yaml`.
- Keep the real file out of Git.
- Unknown keys fail startup.
- Paths under `storage` must remain beneath `root_directory`.
- `media.formats` contains operator-owned semantic format rules. Telegram callbacks store the
  semantic key, never an upstream format ID.
- `media.enabled_sources` is an allowlist applied to normalized extractor families.
- `yt_dlp.cookies_file` is optional. A nonexistent path is ignored by the starter; production work
  should warn explicitly.
- Proxy credentials are secrets and must be redacted from logs.

Generate a JSON schema after changing config models:

```bash
uv run python scripts/export_config_schema.py
```

Every configuration change must update the model, example YAML, tests, schema, and this document.
