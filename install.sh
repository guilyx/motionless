#!/usr/bin/env bash
#
# motionless installer — https://github.com/guilyx/motionless
#
#   curl -fsSL https://raw.githubusercontent.com/guilyx/motionless/main/install.sh | bash
#
# Everything lives inside main(), which is only called on the last line: if the
# download is cut short, nothing runs at all rather than running half a script.
#
# Environment overrides:
#   MOTIONLESS_SOURCE=auto|pypi|git  where to install from (default: auto)
#   MOTIONLESS_REF=<branch|tag>  git ref when installing from git (default: main)
#   MOTIONLESS_SKIP_DEPS=1       do not touch the system package manager
#   MOTIONLESS_YES=1             never prompt (implied when stdin is not a terminal)

set -euo pipefail

REPO_URL="https://github.com/guilyx/motionless"
PACKAGE="motionless-overlay"

main() {
  local skip_deps="${MOTIONLESS_SKIP_DEPS:-0}"
  local assume_yes="${MOTIONLESS_YES:-0}"
  local action="install"

  while [ $# -gt 0 ]; do
    case "$1" in
      --no-deps) skip_deps=1 ;;
      -y|--yes) assume_yes=1 ;;
      --uninstall) action="uninstall" ;;
      -h|--help) usage; return 0 ;;
      *) err "unknown option: $1"; usage; return 2 ;;
    esac
    shift
  done

  if [ "$action" = "uninstall" ]; then
    uninstall
    return 0
  fi

  info "Installing motionless"
  require_linux
  require_python

  if [ "$skip_deps" = "1" ]; then
    warn "skipping system dependencies (--no-deps)"
  else
    install_system_deps "$assume_yes"
  fi

  install_package
  post_install
}

# ----------------------------------------------------------------- utilities

info()  { printf '\033[1;34m::\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ✔\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m !\033[0m %s\n' "$*" >&2; }
err()   { printf '\033[1;31m ✘\033[0m %s\n' "$*" >&2; }
die()   { err "$*"; exit 1; }

has() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'USAGE'
Usage: install.sh [options]

  -y, --yes      do not prompt before installing system packages
      --no-deps  skip system packages (GTK bindings must already be present)
      --uninstall remove motionless (keeps your configuration)
  -h, --help     show this help
USAGE
}

# Prompts have to read /dev/tty: stdin is the piped script itself.
confirm() {
  local prompt="$1"
  # Assume yes when told to, and when there is no terminal to ask on.
  if [ "${2:-0}" = "1" ]; then return 0; fi
  if [ ! -r /dev/tty ]; then return 0; fi
  local reply
  read -r -p "$prompt [Y/n] " reply < /dev/tty || return 0
  case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

SUDO_PRIMED=0

# Ask for the password once, up front, with an explanation. Otherwise sudo
# prompts from inside a long silent command and, since nothing is echoed as
# you type, it looks like the installer has frozen.
prime_sudo() {
  [ "$(id -u)" -eq 0 ] && return 0
  [ "$SUDO_PRIMED" = "1" ] && return 0
  has sudo || die "need root to install packages, but sudo is not available. Re-run as root, or use --no-deps."
  if sudo -n true 2>/dev/null; then
    SUDO_PRIMED=1
    return 0
  fi
  info "sudo needs your password to install system packages."
  warn "nothing is shown as you type — enter it and press Enter"
  sudo -v || die "could not get sudo privileges; re-run as root, or use --no-deps"
  SUDO_PRIMED=1
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    prime_sudo
    sudo "$@"
  fi
}

# --------------------------------------------------------------- preflight

require_linux() {
  [ "$(uname -s)" = "Linux" ] || die "motionless is a Linux program (found $(uname -s))"
}

require_python() {
  has python3 || die "python3 not found; install it and re-run"
  python3 - <<'PY' || die "motionless needs Python 3.10 or newer"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  ok "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
}

# --------------------------------------------------------- system packages

# gtk-layer-shell gives a native Wayland overlay. GNOME does not support the
# protocol, so on GNOME we do not pull the package in and fall back to XWayland.
wants_layer_shell() {
  [ "${XDG_SESSION_TYPE:-}" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ] || return 1
  case "$(printf '%s' "${XDG_CURRENT_DESKTOP:-}" | tr '[:lower:]' '[:upper:]')" in
    *GNOME*) return 1 ;;
    *) return 0 ;;
  esac
}

install_system_deps() {
  local assume_yes="$1"
  local manager packages

  if has apt-get; then
    manager="apt"
    packages="python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-venv pipx"
    if wants_layer_shell; then packages="$packages gir1.2-gtklayershell-0.1"; fi
  elif has dnf; then
    manager="dnf"
    packages="python3-gobject gtk3 pipx"
    if wants_layer_shell; then packages="$packages gtk-layer-shell"; fi
  elif has pacman; then
    manager="pacman"
    packages="python-gobject gtk3 python-pipx"
    if wants_layer_shell; then packages="$packages gtk-layer-shell"; fi
  elif has zypper; then
    manager="zypper"
    packages="python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 python3-pipx"
    if wants_layer_shell; then packages="$packages typelib-1_0-GtkLayerShell-0_1"; fi
  else
    warn "unrecognised package manager; install the GTK 3 Python bindings yourself"
    warn "then re-run with --no-deps"
    return 0
  fi

  info "System packages ($manager): $packages"
  if [ "$(id -u)" -ne 0 ]; then
    info "installing these needs root, so sudo will ask for your password"
  fi
  if ! confirm "Install them now?" "$assume_yes"; then
    warn "skipped; motionless will not start until the GTK bindings are present"
    return 0
  fi

  prime_sudo

  # shellcheck disable=SC2086  # $packages is a deliberate word list
  case "$manager" in
    apt)
      info "updating package lists"
      run_root env DEBIAN_FRONTEND=noninteractive apt-get update -q </dev/null
      info "installing packages"
      run_root env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
        apt-get install -y $packages </dev/null
      ;;
    dnf)    run_root dnf install -y $packages </dev/null ;;
    pacman) run_root pacman -S --needed --noconfirm $packages </dev/null ;;
    zypper) run_root zypper install -y $packages </dev/null ;;
  esac
  ok "system packages installed"
}

# -------------------------------------------------------------- the package

# The venv must see the distro's PyGObject: it cannot be built reliably by pip.
pipx_install() {
  local target="$1"
  pipx install --system-site-packages --force "$target"
}

venv_install() {
  local target="$1"
  local venv="${XDG_DATA_HOME:-$HOME/.local/share}/motionless/venv"
  info "pipx not available; using a virtualenv at $venv"
  python3 -m venv --system-site-packages "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install --quiet "$target"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$venv/bin/motionless" "$HOME/.local/bin/motionless"
}

# Is the package published? Cheap GET against the JSON API, so we never hand
# pip a name it cannot resolve just to discover it does not exist yet.
pypi_has_package() {
  local name="$1"
  if has curl; then
    curl -fsS --max-time 10 -o /dev/null "https://pypi.org/pypi/$name/json" 2>/dev/null
  elif has wget; then
    wget -q --timeout=10 -O /dev/null "https://pypi.org/pypi/$name/json" 2>/dev/null
  else
    return 1
  fi
}

install_target() {
  case "${MOTIONLESS_SOURCE:-pypi}" in
    git) printf 'git+%s@%s' "$REPO_URL" "${MOTIONLESS_REF:-main}" ;;
    *)   printf '%s' "$PACKAGE" ;;
  esac
}

install_package() {
  local installer=venv_install
  if has pipx; then installer=pipx_install; fi

  # "auto" asks PyPI whether the package exists yet; git and pypi force it.
  local source="${MOTIONLESS_SOURCE:-auto}"
  if [ "$source" = "auto" ]; then
    if pypi_has_package "$PACKAGE"; then
      source=pypi
    else
      info "not published to PyPI yet; installing from the git repository"
      source=git
    fi
  fi

  local target
  target="$(MOTIONLESS_SOURCE="$source" install_target)"
  info "Installing $target"

  if ! "$installer" "$target"; then
    if [ "$source" = "pypi" ]; then
      warn "install from PyPI failed; falling back to the git repository"
      "$installer" "$(MOTIONLESS_SOURCE=git install_target)" || die "installation failed"
    else
      die "installation failed"
    fi
  fi
  if has pipx; then pipx ensurepath >/dev/null 2>&1 || true; fi
  ok "motionless installed"
}

post_install() {
  local bin="$HOME/.local/bin"
  export PATH="$bin:$PATH"

  case ":$PATH:" in
    *":$bin:"*) ;;
    *) warn "$bin is not on your PATH; add it to your shell profile" ;;
  esac

  echo
  if has motionless; then
    motionless doctor || true
    echo
    info "Next steps"
    echo "    motionless start            # run it now"
    echo "    motionless toggle           # bind this to a hotkey"
    echo "    motionless service install  # start with your session"
    echo "    motionless config edit      # tune the look"
  else
    warn "the 'motionless' command is not on your PATH yet — open a new shell and try again"
  fi
}

uninstall() {
  info "Removing motionless"
  if has motionless; then
    motionless service uninstall >/dev/null 2>&1 || true
    motionless stop >/dev/null 2>&1 || true
  fi
  if has pipx; then
    pipx uninstall "$PACKAGE" >/dev/null 2>&1 || true
  fi
  rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/motionless"
  rm -f "$HOME/.local/bin/motionless"
  ok "removed (configuration in ${XDG_CONFIG_HOME:-$HOME/.config}/motionless was kept)"
}

main "$@"
