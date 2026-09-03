# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Configuration frontend. All reads/edits go through the typed application
# CLI; the sanitized summary prints only booleans, counts, and non-secret
# scalars.
# --------------------------------------------------------------------------- #

config_summary() {
  echo "Sanitized configuration summary (no secrets):"
  config_edit_run telegram-status 2>/dev/null | sed 's/^/  /' || echo "  unavailable"
  echo "  logger:"
  config_edit_run logger-status 2>/dev/null | sed 's/^/    /' || true
  echo "  cookie file:"
  config_edit_run cookie-status 2>/dev/null | sed 's/^/    /' || true
  local max_file media_max queue_max
  max_file="$(config_edit_run get telegram.max_upload_size_mb 2>/dev/null | sed 's/.*: //' || echo unknown)"
  media_max="$(config_edit_run get media.max_file_size_mb 2>/dev/null | sed 's/.*: //' || echo unknown)"
  queue_max="$(config_edit_run get queue.max_jobs 2>/dev/null | sed 's/.*: //' || echo unknown)"
  echo "  telegram.max_upload_size_mb: ${max_file:-unknown}"
  echo "  media.max_file_size_mb: ${media_max:-unknown}"
  echo "  queue.max_jobs: ${queue_max:-unknown}"
}

config_edit_key() {
  local key="${1:-}"
  [[ -n "$key" ]] || {
    echo "Usage: tmb config set KEY VALUE | get KEY | list-add KEY VALUE | list-remove KEY VALUE" >&2
    return 2
  }
  config_edit_run "$@"
}

config_set_secret_key() {
  local key="${1:-}" value
  [[ -n "$key" ]] || {
    echo "Usage: tmb config set-secret KEY (reads the value from stdin)" >&2
    return 2
  }
  value="$(hidden_input "New value for $key (input hidden): ")" || return 1
  printf '%s' "$value" | config_edit_run set "$key" -
}

config_wizard() {
  # Backward-compatible interactive full wizard.
  docker run --rm -it --user "$(id -u):$(id -g)" \
    -v "$ROOT_DIR:/workspace" -w /workspace \
    "$(configured_image)" telegram-media-bot configure --config /workspace/config.yaml
}

run_config() {
  local action="${1:-menu}"
  case "$action" in
    check)
      run_config_check
      ;;
    show|summary)
      config_summary
      ;;
    wizard)
      config_wizard
      ;;
    set|get|list-add|list-remove)
      shift
      config_edit_key "$action" "$@"
      ;;
    set-secret)
      shift
      config_set_secret_key "$1"
      ;;
    *)
      echo "Usage: tmb config check|show|wizard|set KEY VALUE|get KEY|list-add KEY VALUE|list-remove KEY VALUE|set-secret KEY" >&2
      return 2
      ;;
  esac
}