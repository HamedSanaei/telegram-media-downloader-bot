# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Project-scoped Docker management. Cleanup targets only the project image
# repository and only images not referenced by any container; unrelated
# images, volumes, and caches are never touched.
# --------------------------------------------------------------------------- #

run_docker_status() {
  docker info --format 'Server: {{.ServerVersion}} | Containers: {{.Containers}} | Images: {{.Images}} | Version: {{.ServerVersion}}' 2>/dev/null ||
    docker info 2>/dev/null | grep -E 'Server Version|Containers:|Images:' || true
}

run_docker_version() {
  docker version --format '{{.Server.Version}}' 2>/dev/null || docker version 2>/dev/null || true
}

run_docker_containers() {
  compose --profile local-api ps -a
}

run_docker_images() {
  docker image ls --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}' | head -n 40
}

run_docker_current_image() {
  local image digest
  image="$(configured_image)"
  digest="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}none{{end}}' "$image" 2>/dev/null || true)"
  echo "Configured image: $image"
  echo "Digest: ${digest:-unknown}"
}

run_docker_pull_current() {
  docker pull "$(configured_image)"
}

run_docker_pull_latest_release() {
  local tag
  tag="${TMB_RELEASE_TAG:-latest}"
  docker pull "$IMAGE_REPOSITORY:$tag"
}

run_docker_recreate() {
  compose --profile local-api up -d --no-build --force-recreate
}

run_docker_volumes() {
  docker volume ls --format 'table {{.Name}}\t{{.Driver}}' | grep -E 'VOLUME|telegram-media-downloader' || docker volume ls
}

run_docker_cleanup_preview() {
  cleanup_project_resources true
}

run_docker_cleanup_old_images() {
  local assume_yes=0
  if [[ "${1:-}" == "--yes" ]]; then
    assume_yes=1
  fi
  local candidates
  candidates="$(cleanup_project_resources true | grep -c 'Would remove old project image' || true)"
  if [[ "$candidates" -eq 0 ]]; then
    echo "No old unreferenced project images to remove."
    return 0
  fi
  echo "This removes up to $candidates old unreferenced image(s) from $IMAGE_REPOSITORY only."
  if [[ "$assume_yes" != "1" ]]; then
    require_confirmation "DELETE-OLD-IMAGES" || return 1
  fi
  cleanup_project_resources false
}

run_docker_compose_config() {
  compose --profile local-api config
}

run_docker_build_local() {
  docker build -t "$IMAGE_REPOSITORY:local-build" .
}

# tmb docker ------------------------------------------------------------
run_docker() {
  local action="${1:-status}"
  shift || true
  case "$action" in
    status) run_docker_status ;;
    version) run_docker_version ;;
    compose-version)
      docker compose version 2>/dev/null || true
      ;;
    containers|ps) run_docker_containers ;;
    images) run_docker_images ;;
    current-image) run_docker_current_image ;;
    digest) run_docker_current_image ;;
    pull) run_docker_pull_current ;;
    pull-latest) run_docker_pull_latest_release ;;
    recreate) run_docker_recreate ;;
    volumes) run_docker_volumes ;;
    cleanup-preview) run_docker_cleanup_preview ;;
    cleanup-old-images) run_docker_cleanup_old_images "${1:-}" ;;
    compose-config) run_docker_compose_config ;;
    build) run_docker_build_local ;;
    *)
      echo "Usage: tmb docker status|version|compose-version|containers|images|current-image|digest|pull|pull-latest|recreate|volumes|cleanup-preview|cleanup-old-images [--yes]|compose-config|build" >&2
      return 2
      ;;
  esac
}