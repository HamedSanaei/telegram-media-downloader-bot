# Operations

## Start

```bash
./manage.sh init
# edit config.yaml
./manage.sh up
```

## Observe

```bash
./manage.sh status
./manage.sh logs
./manage.sh logs worker
```

## Validate before deployment

```bash
./manage.sh config-check
./manage.sh check
```

## Update yt-dlp

```bash
git switch -c chore/update-ytdlp
./manage.sh upgrade-ytdlp
./manage.sh check
git diff -- pyproject.toml uv.lock
./manage.sh restart
```

Do not update dependencies inside a running container. Build a new image from the reviewed lockfile.

## Rollback

Revert the dependency update commit or restore the previous `uv.lock`, then run:

```bash
./manage.sh restart
```

## Data

Redis stores transient queue state in its Docker volume. Download and temporary files live under
`./data`. The current starter has no durable business database.
