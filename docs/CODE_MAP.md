# Code map

| Path | Responsibility |
|---|---|
| `src/telegram_media_bot/domain/` | Stable models, enums, identifiers, and exceptions |
| `src/telegram_media_bot/application/ports/` | Interfaces required by use cases |
| `src/telegram_media_bot/application/services/` | Orchestrates inspection and downloading |
| `src/telegram_media_bot/infrastructure/ytdlp/` | The only direct yt-dlp integration |
| `src/telegram_media_bot/infrastructure/queue/` | ARQ queue client implementation |
| `src/telegram_media_bot/telegram/` | Handlers, routers, text presentation, upload adapter |
| `src/telegram_media_bot/workers/` | ARQ worker settings and job functions |
| `src/telegram_media_bot/bootstrap/` | Config, logging, and composition roots |
| `tests/unit/` | Fast deterministic tests |
| `tests/integration/` | Local integration tests with fakes/Redis where available |
| `tests/contracts/` | Opt-in external yt-dlp smoke tests |
| `docs/tasks/` | Ordered implementation tasks for Codex |

## Upstream compatibility hot spots

- `infrastructure/ytdlp/engine.py`: `YoutubeDL` lifecycle and calls;
- `infrastructure/ytdlp/options.py`: semantic mode to yt-dlp option mapping;
- `infrastructure/ytdlp/mapper.py`: upstream metadata to `MediaInfo`;
- `infrastructure/ytdlp/error_mapper.py`: upstream errors to project exceptions.
