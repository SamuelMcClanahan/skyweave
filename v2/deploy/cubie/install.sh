#!/usr/bin/env bash
# Install the SkyWeave drone stack on the Radxa Cubie A7S (contract B-D3).
# Read README.md in this directory first.
#
#   sudo ./install.sh [--source PATH] [--extras LIST] [--skip-apt] [--dry-run]
#   sudo ./install.sh --uninstall [--purge] [--dry-run]
#
# PATH is a bundle from make-bundle.sh (no internet necessary for Python
# packages) or a repository checkout (uv gets Python and the packages from
# the internet). PATH can be a directory or a .tar / .tar.gz / .tgz file.
# Without --source, the script uses the bundle or checkout that it is in.
#
# It is safe to run this script again. It never overwrites
# /etc/skyweave/skyweave.env and never prints or stores the UI token.
set -euo pipefail
umask 022
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

readonly PREFIX=/opt/skyweave
readonly ETC_DIR=/etc/skyweave
readonly STATE_DIR=/var/lib/skyweave
readonly ENV_FILE=$ETC_DIR/skyweave.env
readonly SERVICE_USER=skyweave
readonly UNIT_DIR=/etc/systemd/system
readonly UDEV_RULE_NAME=60-skyweave.rules
readonly UDEV_RULE=/etc/udev/rules.d/$UDEV_RULE_NAME
readonly UNITS=(skyweave-companion.service skyweave-percepd.service)
readonly PYTHON_SERIES=3.10
readonly SITE_DIR=$PREFIX/venv/lib/python$PYTHON_SERIES/site-packages
# OS packages. None is necessary for the Python environment itself.
# iproute2 and udev: used by skyweave-run and the camera rule.
# v4l-utils and rsync: used by the README procedures.
readonly APT_PACKAGES=(ca-certificates iproute2 udev v4l-utils rsync)
# The board has no real-time clock. A clock before this time is wrong
# (2026-11-01 00:00 UTC). A bundle gives a later value (its build time).
CLOCK_FLOOR_EPOCH=1793491200

SOURCE=""
EXTRAS=drone # repository-checkout mode only; a bundle has its own list
DRY_RUN=0
UNINSTALL=0
PURGE=0
SKIP_APT=0
TMP_DIR=""
KIND=""        # bundle | repo
BUNDLE_DIR=""  # bundle root
PROJECT_DIR="" # directory with pyproject.toml, uv.lock and src/
SRC_DIR=""     # directory that contains the package directory skyweave2/
REVISION=unknown
FIRST_INSTALL=0
CURRENT_STEP="start"

# --- output --------------------------------------------------------------------

info() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
  printf '\nERROR: %s\n' "$*" >&2
  EXPECTED_EXIT=1
  exit 1
}
step() {
  CURRENT_STEP=$*
  printf '\n==> %s\n' "$*"
}

# run COMMAND...: do the command, or print it in a dry run.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    would run:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# write_file PATH TEXT: make a small text file.
write_file() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    would write %s: %s\n' "$1" "$2"
  else
    printf '%s\n' "$2" >"$1"
  fi
}

# The EXIT trap removes the temporary directory. If a command failed without
# its own message, it also tells the operator which step did not finish.
EXPECTED_EXIT=0
on_exit() {
  local status=$?
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
  if [ "$status" -ne 0 ] && [ "$EXPECTED_EXIT" -eq 0 ]; then
    printf '\nERROR: install.sh stopped in the step "%s" (exit code %s).\n' \
      "$CURRENT_STEP" "$status" >&2
    printf 'Read the messages above. Correct the cause. Then run install.sh again.\n' >&2
  fi
}
trap on_exit EXIT

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- arguments -----------------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --source)
      [ $# -ge 2 ] || die "--source needs a path."
      SOURCE=$2
      shift 2
      ;;
    --extras)
      [ $# -ge 2 ] || die "--extras needs a list, for example: drone,percepd"
      EXTRAS=$2
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    --purge)
      PURGE=1
      shift
      ;;
    --skip-apt)
      SKIP_APT=1
      shift
      ;;
    -h | --help)
      usage
      EXPECTED_EXIT=1
      exit 0
      ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done
if [ "$PURGE" -eq 1 ] && [ "$UNINSTALL" -eq 0 ]; then
  die "--purge is only for --uninstall."
fi

# --- checks --------------------------------------------------------------------

# A real install stops on a failed check. A dry run prints it and continues.
check_fail() {
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "$* (a real install stops here)"
  else
    die "$*"
  fi
}

check_platform() {
  step "Check the platform"
  [ "$(uname -s)" = Linux ] || check_fail "this is not Linux (uname -s: $(uname -s))."
  [ "$(uname -m)" = aarch64 ] || check_fail "this is not aarch64 (uname -m: $(uname -m))."
  [ "$(id -u)" -eq 0 ] || check_fail "run this script as root (sudo)."
  local os=unknown
  if [ -r /etc/os-release ]; then
    os=$(sed -n 's/^PRETTY_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' /etc/os-release)
  fi
  info "OS: $os, kernel: $(uname -r), machine: $(uname -m)"
}

clock_is_wrong() { [ "$(date +%s)" -lt "$CLOCK_FLOOR_EPOCH" ]; }

have_systemd() { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }

# --- source --------------------------------------------------------------------

resolve_source() {
  step "Find the source"
  if [ -z "$SOURCE" ]; then
    if [ -f "$SCRIPT_DIR/../BUNDLE_MANIFEST" ]; then
      SOURCE=$(cd "$SCRIPT_DIR/.." && pwd)
    elif [ -f "$SCRIPT_DIR/../../uv.lock" ]; then
      SOURCE=$(cd "$SCRIPT_DIR/../.." && pwd)
    else
      die "no source found near $SCRIPT_DIR. Give one with --source PATH."
    fi
  fi
  [ -e "$SOURCE" ] || die "the source $SOURCE does not exist."

  local root=$SOURCE
  if [ -f "$SOURCE" ]; then
    case "$SOURCE" in
      *.tar | *.tar.gz | *.tgz) ;;
      *) die "the source file $SOURCE is not a .tar, .tar.gz or .tgz file." ;;
    esac
    if [ -f "$SOURCE.sha256" ]; then
      info "check $SOURCE against $SOURCE.sha256"
      (cd "$(dirname "$SOURCE")" && sha256sum -c "$(basename "$SOURCE").sha256" >/dev/null) ||
        die "the checksum of $SOURCE is wrong. Copy the file to the board again."
    fi
    TMP_DIR=$(mktemp -d /var/tmp/skyweave-install.XXXXXX)
    info "extract $SOURCE to the temporary directory $TMP_DIR"
    tar -xf "$SOURCE" -C "$TMP_DIR" --no-same-owner ||
      die "cannot extract $SOURCE. Is the file complete? Copy it again."
    root=$TMP_DIR
    # An archive usually has one top directory.
    local entries=("$TMP_DIR"/*)
    if [ "${#entries[@]}" -eq 1 ] && [ -d "${entries[0]}" ]; then root=${entries[0]}; fi
  fi
  root=$(cd "$root" && pwd)

  if [ -f "$root/BUNDLE_MANIFEST" ]; then
    KIND=bundle
    BUNDLE_DIR=$root
    SRC_DIR=$root/src
  elif [ -f "$root/v2/uv.lock" ]; then
    KIND=repo
    PROJECT_DIR=$root/v2
    SRC_DIR=$root/v2/src
  elif [ -f "$root/uv.lock" ] && [ -f "$root/pyproject.toml" ]; then
    KIND=repo
    PROJECT_DIR=$root
    SRC_DIR=$root/src
  else
    die "$SOURCE is not a bundle (no BUNDLE_MANIFEST) and not a repository" \
      "checkout (no v2/uv.lock)."
  fi
  [ -f "$SRC_DIR/skyweave2/drone/companiond.py" ] ||
    die "$SRC_DIR/skyweave2/drone/companiond.py is missing. The source has no drone stack."

  if [ "$KIND" = bundle ]; then
    local format target epoch
    format=$(manifest_value format)
    [ "$format" = skyweave-cubie-bundle-1 ] ||
      die "unknown bundle format '$format'. Make the bundle and install.sh from one revision."
    target=$(manifest_value target)
    [ "$target" = linux-aarch64-gnu ] || die "the bundle is for '$target', not linux-aarch64-gnu."
    REVISION=$(manifest_value revision)
    epoch=$(manifest_value build_epoch)
    case "$epoch" in '' | *[!0-9]*) ;; *) if [ "$epoch" -gt "$CLOCK_FLOOR_EPOCH" ]; then CLOCK_FLOOR_EPOCH=$epoch; fi ;; esac
    info "source: bundle $BUNDLE_DIR"
    info "extras in the bundle: $(manifest_value extras)"
  else
    if command -v git >/dev/null 2>&1; then
      REVISION=$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null) || REVISION=unknown
    fi
    info "source: repository checkout, project directory $PROJECT_DIR"
    info "extras: $EXTRAS"
  fi
  info "revision: $REVISION"
}

manifest_value() {
  sed -n "s/^$1=//p" "$BUNDLE_DIR/BUNDLE_MANIFEST" | head -n 1
}

# --- install steps -------------------------------------------------------------

check_clock() {
  step "Check the clock"
  info "board time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  if ! clock_is_wrong; then
    info "the clock is not before the source date. OK."
    return 0
  fi
  local msg="the board clock is in the past (the board has no real-time clock)."
  msg="$msg Downloads fail with certificate or 'not valid yet' errors until it is correct."
  msg="$msg Set it: date -u -s 'YYYY-MM-DD HH:MM:SS', or let the network time service set it."
  if [ "$KIND" = repo ]; then
    check_fail "$msg"
  else
    warn "$msg"
    warn "The bundle install does not need the clock. Only the OS package step does."
  fi
}

install_os_packages() {
  step "Install OS packages"
  if [ "$SKIP_APT" -eq 1 ]; then
    info "skipped (--skip-apt)."
    return 0
  fi
  if ! command -v dpkg-query >/dev/null 2>&1 || ! command -v apt-get >/dev/null 2>&1; then
    warn "this OS has no apt. Install these packages yourself: ${APT_PACKAGES[*]}"
    return 0
  fi
  local missing=() pkg
  for pkg in "${APT_PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
      missing+=("$pkg")
    fi
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    info "all packages are installed (${APT_PACKAGES[*]}). No internet is necessary."
    return 0
  fi
  info "missing: ${missing[*]} (this step needs internet)"
  if [ "$DRY_RUN" -eq 1 ]; then
    run apt-get update
    run apt-get install -y --no-install-recommends "${missing[@]}"
    return 0
  fi
  if DEBIAN_FRONTEND=noninteractive apt-get update &&
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"; then
    return 0
  fi
  warn "apt could not install: ${missing[*]}."
  warn "The install continues. These packages are for the README procedures and for"
  warn "the checks in skyweave-run. Install them when the board has internet:"
  warn "  apt-get install ${missing[*]}"
}

create_user_and_dirs() {
  step "Create the service user and the directories"
  if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
    info "user $SERVICE_USER exists."
  else
    run useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home \
      --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  local group
  for group in dialout video; do
    if getent group "$group" >/dev/null 2>&1; then
      run usermod -a -G "$group" "$SERVICE_USER"
    else
      warn "the group $group does not exist. Serial (dialout) or camera (video) access" \
        "can fail. See the README sections 'Find the UART' and 'Camera'."
    fi
  done
  run install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/bin" "$ETC_DIR"
  run install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR" "$STATE_DIR/recordings"
  run install -d -m 0755 -o root -g root "$STATE_DIR/models"
}

# copy_tree FROM_PARENT TO_PARENT NAME...: copy directories, owned by root.
copy_tree() {
  local from=$1 to=$2
  shift 2
  if [ "$DRY_RUN" -eq 1 ]; then
    info "would copy from $from to $to: $*"
    return 0
  fi
  tar -C "$from" --exclude=__pycache__ -cf - "$@" | tar -C "$to" --no-same-owner -xf -
  chmod -R go-w,a+rX "$to"
}

install_python_env_bundle() {
  step "Install Python $PYTHON_SERIES and the locked packages (from the bundle, no internet)"
  local python_dir requirements stamp_file stamp
  python_dir=$(manifest_value python_dir)
  requirements=$BUNDLE_DIR/requirements.txt
  stamp_file=$PREFIX/venv/.skyweave-bundle-stamp
  [ -x "$BUNDLE_DIR/python/$python_dir/bin/python$PYTHON_SERIES" ] ||
    die "the bundle has no Python at python/$python_dir. Make the bundle again."
  [ -f "$requirements" ] && [ -d "$BUNDLE_DIR/wheels" ] ||
    die "the bundle has no requirements.txt or no wheels/. Make the bundle again."
  stamp="$python_dir $(sha256sum "$requirements" | cut -d' ' -f1)"

  if [ -f "$stamp_file" ] && [ "$(cat "$stamp_file")" = "$stamp" ] &&
    "$PREFIX/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
    info "the environment in $PREFIX/venv has these packages already. No change."
    info "(To build it again, delete $PREFIX/venv and run install.sh again.)"
    return 0
  fi

  run rm -rf "$PREFIX/venv" "$PREFIX/python"
  run install -d -m 0755 "$PREFIX/python"
  copy_tree "$BUNDLE_DIR/python" "$PREFIX/python" "$python_dir"
  run "$PREFIX/python/$python_dir/bin/python$PYTHON_SERIES" -m venv "$PREFIX/venv"
  run "$PREFIX/venv/bin/python" -m pip install \
    --no-index --find-links "$BUNDLE_DIR/wheels" --require-hashes --only-binary=:all: \
    --disable-pip-version-check --no-warn-script-location --quiet \
    -r "$requirements"
  write_file "$stamp_file" "$stamp"
}

install_python_env_repo() {
  step "Install Python $PYTHON_SERIES and the locked packages (uv, from the internet)"
  if ! command -v uv >/dev/null 2>&1; then
    check_fail "uv is not installed on this board. Use a bundle instead (README," \
      "'Make the bundle'), or install uv (https://docs.astral.sh/uv/)."
  fi
  local extra_args=() extras_list=() extra
  IFS=, read -r -a extras_list <<<"$EXTRAS"
  for extra in ${extras_list[@]+"${extras_list[@]}"}; do
    if [ -n "$extra" ]; then extra_args+=(--extra "$extra"); fi
  done
  # uv gets a managed CPython for this board and the packages of uv.lock.
  # --frozen: uv.lock is not changed. --no-install-project: nothing is
  # written to the source directory; the code is copied in the next step.
  run env UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_PROJECT_ENVIRONMENT="$PREFIX/venv" \
    UV_LINK_MODE=copy UV_NO_CACHE=1 UV_PYTHON_PREFERENCE=only-managed \
    uv sync --project "$PROJECT_DIR" --frozen --no-dev --no-install-project \
    --compile-bytecode --python "$PYTHON_SERIES" ${extra_args[@]+"${extra_args[@]}"}
}

install_app() {
  step "Install the drone stack code"
  run rm -rf "$PREFIX/app.new"
  run install -d -m 0755 "$PREFIX/app.new/src"
  local names=(skyweave2)
  if [ -d "$SRC_DIR/skyweave2.egg-info" ]; then names+=(skyweave2.egg-info); fi
  copy_tree "$SRC_DIR" "$PREFIX/app.new/src" "${names[@]}"
  write_file "$PREFIX/app.new/REVISION" "$REVISION"
  run rm -rf "$PREFIX/app"
  run mv "$PREFIX/app.new" "$PREFIX/app"
  # The environment finds the code through this path file.
  write_file "$SITE_DIR/skyweave2.pth" "$PREFIX/app/src"
  run "$PREFIX/venv/bin/python" -m compileall -q "$PREFIX/app/src"

  step "Test the installed code"
  if [ "$DRY_RUN" -eq 1 ]; then
    run "$PREFIX/venv/bin/python" -m skyweave2.drone.companiond --help
    return 0
  fi
  local version
  version=$("$PREFIX/venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  [ "$version" = "$PYTHON_SERIES" ] || die "the environment has Python $version, not $PYTHON_SERIES."
  "$PREFIX/venv/bin/python" -m skyweave2.drone.companiond --help >/dev/null ||
    die "'python -m skyweave2.drone.companiond --help' failed in $PREFIX/venv."
  info "python -m skyweave2.drone.companiond --help: OK (Python $version)"
  if "$PREFIX/venv/bin/python" -c \
    'import importlib.util as u, sys; sys.exit(0 if u.find_spec("skyweave2.drone.percepd") else 1)' \
    >/dev/null 2>&1; then
    info "module skyweave2.drone.percepd: found"
  else
    warn "this source has no module skyweave2.drone.percepd. skyweave-percepd.service" \
      "stops with a configuration error until you install a source that has it."
  fi
}

install_files() {
  step "Install the launcher, the service files, the udev rule and the configuration"
  local f
  for f in skyweave-run skyweave.env.template "$UDEV_RULE_NAME" README.md "${UNITS[@]}"; do
    [ -f "$SCRIPT_DIR/$f" ] || die "$SCRIPT_DIR/$f is missing. The deploy directory is not complete."
  done
  run install -m 0755 -o root -g root "$SCRIPT_DIR/skyweave-run" "$PREFIX/bin/skyweave-run"
  run install -m 0644 -o root -g root "$SCRIPT_DIR/README.md" "$PREFIX/README.md"
  for f in "${UNITS[@]}"; do
    run install -m 0644 -o root -g root "$SCRIPT_DIR/$f" "$UNIT_DIR/$f"
  done
  if [ -d "$(dirname "$UDEV_RULE")" ]; then
    run install -m 0644 -o root -g root "$SCRIPT_DIR/$UDEV_RULE_NAME" "$UDEV_RULE"
  else
    warn "$(dirname "$UDEV_RULE") does not exist (no udev?). The camera rule is not installed."
  fi

  run install -m 0644 -o root -g root "$SCRIPT_DIR/skyweave.env.template" "$ENV_FILE.template"
  if [ -e "$ENV_FILE" ]; then
    info "$ENV_FILE exists. It is NOT changed (it holds your token and settings)."
    run chown root:root "$ENV_FILE"
    run chmod 0600 "$ENV_FILE"
    # Names only, never values.
    local name missing=()
    while IFS= read -r name; do
      grep -q "^$name=" "$ENV_FILE" || missing+=("$name")
    done < <(sed -n 's/^\([A-Z][A-Z0-9_]*\)=.*/\1/p' "$SCRIPT_DIR/skyweave.env.template")
    if [ "${#missing[@]}" -gt 0 ]; then
      warn "$ENV_FILE does not have these variables of the new template: ${missing[*]}."
      warn "Copy them from $ENV_FILE.template and set them."
    fi
  else
    FIRST_INSTALL=1
    info "make $ENV_FILE from the template (owner root, mode 0600, NO token)."
    run install -m 0600 -o root -g root "$SCRIPT_DIR/skyweave.env.template" "$ENV_FILE"
  fi
}

activate() {
  step "Tell systemd and udev"
  if command -v udevadm >/dev/null 2>&1 && [ -d /run/udev ]; then
    run udevadm control --reload-rules || warn "udevadm could not load the rules again."
    run udevadm trigger --subsystem-match=video4linux --action=change ||
      warn "udevadm trigger failed. Connect the camera again, or start the board again."
  else
    warn "udev is not running here. The camera rule applies after the next start of the board."
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl is not here. The services are installed but not enabled."
    return 0
  fi
  if have_systemd; then run systemctl daemon-reload; fi
  run systemctl enable "${UNITS[@]}"
  if have_systemd && [ "$FIRST_INSTALL" -eq 0 ]; then
    info "start the services again if they are in operation (new code)."
    run systemctl try-restart "${UNITS[@]}"
  fi
}

finish_message() {
  cat <<EOF

==> Done. Installed revision: $REVISION
EOF
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    This was a dry run. Nothing on the board was changed.\n'
    return 0
  fi
  if [ "$FIRST_INSTALL" -eq 1 ]; then
    cat <<EOF

The services are enabled but NOT started. Do these steps now (README.md,
section "Configure"):
  1. Edit the configuration:   sudoedit $ENV_FILE
       - SKYWEAVE_UI_TOKEN     set the ground UI token (it is empty)
       - SKYWEAVE_FC_DEVICE    the UART that connects to the FC
       - SKYWEAVE_UI_HOST      the access-point address of the Cubie
       - SKYWEAVE_MODEL        the detector model file
  2. Test the configuration:   sudo $PREFIX/bin/skyweave-run check
  3. Start the services:       sudo systemctl start ${UNITS[*]}
  4. Look at the log:          journalctl -u skyweave-companion -u skyweave-percepd -f
EOF
  else
    cat <<EOF

Test the configuration:   sudo $PREFIX/bin/skyweave-run check
Look at the services:     systemctl status ${UNITS[*]}
EOF
  fi
}

# --- uninstall -----------------------------------------------------------------

uninstall() {
  step "Stop and remove the services"
  local f present=()
  for f in "${UNITS[@]}"; do
    if [ -e "$UNIT_DIR/$f" ]; then present+=("$f"); fi
  done
  if [ "${#present[@]}" -eq 0 ]; then
    info "no SkyWeave service files are installed."
  elif command -v systemctl >/dev/null 2>&1; then
    if have_systemd; then
      run systemctl disable --now "${present[@]}" || warn "systemctl could not stop a service."
    else
      run systemctl disable "${present[@]}" || warn "systemctl could not disable a service."
    fi
  fi
  for f in "${UNITS[@]}"; do run rm -f "$UNIT_DIR/$f"; done
  run rm -f "$UDEV_RULE"
  if have_systemd; then run systemctl daemon-reload; fi
  if command -v udevadm >/dev/null 2>&1 && [ -d /run/udev ]; then
    run udevadm control --reload-rules || true
  fi

  step "Remove the code and the Python environment"
  run rm -rf "$PREFIX"

  if [ "$PURGE" -eq 1 ]; then
    step "Remove the configuration, the recordings, the models and the user (--purge)"
    run rm -rf "$ETC_DIR" "$STATE_DIR"
    if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
      run userdel "$SERVICE_USER" || warn "could not remove the user $SERVICE_USER."
    fi
    printf '\n==> Done. SkyWeave is removed from this board.\n'
  else
    cat <<EOF

==> Done. These stay on the board (remove them with --uninstall --purge):
    $ETC_DIR      configuration and UI token
    $STATE_DIR    recordings and models
    user $SERVICE_USER
EOF
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    This was a dry run. Nothing on the board was changed.\n'
  fi
}

# --- main ----------------------------------------------------------------------

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'DRY RUN: this prints the steps. It changes nothing on the board.\n'
fi
check_platform
if [ "$UNINSTALL" -eq 1 ]; then
  uninstall
  exit 0
fi
resolve_source
check_clock
install_os_packages
create_user_and_dirs
if [ "$KIND" = bundle ]; then
  install_python_env_bundle
else
  install_python_env_repo
fi
install_app
install_files
activate
finish_message
