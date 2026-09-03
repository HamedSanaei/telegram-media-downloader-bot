# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Interactive UI helpers. Colors are used only when stdout is a TTY; no
# functionality depends on ANSI codes.
# --------------------------------------------------------------------------- #

if is_tty; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'
  C_CYAN=$'\033[36m'
else
  C_RESET=""
  C_BOLD=""
  C_RED=""
  C_GREEN=""
  C_YELLOW=""
  C_BLUE=""
  C_CYAN=""
fi

ui_banner() {
  local title="$1"
  printf '\n%s=================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '%s %s %s\n' "$C_BOLD" "$title" "$C_RESET"
  printf '%s=================================================%s\n' "$C_BOLD" "$C_RESET"
}

ui_heading() {
  printf '\n%s--- %s ---%s\n' "$C_CYAN" "$1" "$C_RESET"
}

ui_ok() {
  printf '%sOK  %s%s\n' "$C_GREEN" "$1" "$C_RESET"
}

ui_fail() {
  printf '%sFAIL %s%s\n' "$C_RED" "$1" "$C_RESET"
}

ui_info() {
  printf '%s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"
}

ui_note() {
  printf '%s%s%s\n' "$C_BLUE" "$1" "$C_RESET"
}

ui_status_line() {
  local label="$1" value="$2"
  printf '%-28s %s\n' "$label:" "$value"
}

# Print a menu and read one selection. Returns the selection or 0 for exit.
# The listing goes to stderr so callers that capture stdout with
# selection="$(menu_select ...)" receive ONLY the chosen number; on EOF or
# Back the selection is empty and callers exit/return cleanly.
menu_select() {
  local prompt="$1"
  shift
  local number label choice
  while true; do
    number=1
    for label in "$@"; do
      printf '%s) %s\n' "$number" "$label" >&2
      number=$((number + 1))
    done
    printf '0) Back\n' >&2
    read -r -p "$prompt " choice || return 0
    if [[ "$choice" == "0" ]]; then
      return 0
    fi
    if [[ "$choice" =~ ^[1-9][0-9]*$ ]] && ((choice < number)); then
      printf '%s\n' "$choice"
      return 0
    fi
    echo "Invalid selection: $choice" >&2
  done
}

# menu_select returns 0 with empty output for Back/EOF, so callers use:
#   selection="$(menu_select ...)" || return 0
#   [[ -z "$selection" ]] && return 0