#!/usr/bin/env bash
#
# Adiona-TV update applier — the only thing on the box that is allowed to change
# which release is running.
#
# It exists as a separate script, rather than as code inside adiona-updater.py,
# for one reason: applying an update means restarting adiona-updater itself. A
# child process would die with its parent's cgroup, so adiona-updater.py launches
# this through `systemd-run --unit=adiona-apply`, which makes PID 1 the parent and
# puts it in a sibling cgroup that `systemctl restart adiona-updater` cannot touch.
#
# ── The layout it maintains ──────────────────────────────────────────────────
#
#   /opt/adiona/                 a real directory (as the image build creates it)
#     releases/1.5.0/{web,controller,kiosk,wheel,first-boot,updater,VERSION,
#                     config/box.conf,packages,units/*.service,
#                     units-installed/*.service}
#     releases/1.6.0/...
#     current      -> releases/1.6.0    the ONE symlink, swapped atomically
#     web          -> current/web       compat links, created once at migration
#     controller   -> current/controller and never touched again, so every
#     kiosk        -> current/kiosk     absolute path already baked into the
#     wheel        -> current/wheel     unit files and the kiosk scripts keeps
#     first-boot   -> current/first-boot working unchanged
#     updater      -> current/updater
#     VERSION      -> current/VERSION
#
# `/opt/adiona` itself is deliberately NOT the symlink. rename(2) refuses to
# replace a populated directory with a symlink (EISDIR), so converting it would
# mean `rm -rf /opt/adiona && ln -s ...` — and a power cut inside that window
# leaves a box with no /opt/adiona at all, i.e. all four services dead and a black
# screen recoverable only over SSH. Moving the symlink one level down makes the
# swap a single rename(2) of a symlink over a symlink, which is atomic.
#
# `units/` holds the unit files SHIPPED by that release. `units-installed/` holds
# the unit files that were live in /etc/systemd/system when that release was made
# current — that is the restore source on rollback, since units live outside the
# release tree. `packages` is one apt package name per line, written by
# adiona-updater.py from the manifest; absent or empty means no apt work.
#
# adiona-updater.py builds this directory from the OTA tarball (which carries the
# repo layout: web/ controller/ system/ config/ VERSION) and hands it over
# complete. This script never downloads or unpacks anything.
#
# ── Entry points ─────────────────────────────────────────────────────────────
#
#   --protocol 1 --from X --to Y --release-dir D   apply, health-check, roll back
#   --boot-check                                   finish or undo an interrupted apply
#   --migrate                                      flat layout -> release layout
#   --rollback                                     manual revert to the previous release
#
# ── Crash safety ─────────────────────────────────────────────────────────────
#
# /etc/adiona/update-pending is written and fsync'd (file AND containing
# directory) BEFORE the swap. ext4's data=ordered gives no ordering guarantee
# between two different files, so without the explicit directory fsync you can end
# up with a durable swap and a lost marker — exactly the unprotected state the
# marker exists to prevent. adiona-rollback.service runs --boot-check whenever the
# marker survives a boot, before any adiona service starts.

set -euo pipefail

# Every path is overridable, purely so the layout, migration and rollback logic
# can be exercised in a sandbox on a workstation instead of only on a live box —
# the same convention adiona_controller.py and adiona-wheel.py already use for
# ADIONA_CONF / ADIONA_WEB_DIR / ADIONA_RUN_DIR. Nothing in production sets these.
ROOT="${ADIONA_ROOT:-/opt/adiona}"
ETC="${ADIONA_ETC:-/etc/adiona}"
STATE_DIR="${ADIONA_STATE_DIR:-/var/lib/adiona}"
RUN_DIR="${ADIONA_RUN_DIR:-/run/adiona}"
SYSTEMD_DIR="${ADIONA_SYSTEMD_DIR:-/etc/systemd/system}"
STATE_URL="${ADIONA_STATE_URL:-http://127.0.0.1:8090/state}"

RELEASES="$ROOT/releases"
CURRENT="$ROOT/current"
MARKER="$ETC/update-pending"
RESULT="$STATE_DIR/last-apply.json"
UPDATED_STAMP="$ETC/.updated"
LOCK="$ROOT/.update.lock"
PHASE_FILE="$RUN_DIR/apply.phase"

# The names under /opt/adiona that become symlinks into current/.
LINKED="web controller kiosk wheel first-boot updater VERSION"
# The services this script may restart, in dependency order.
UNITS="adiona-controller adiona-wheel adiona-updater adiona-kiosk"

# How long to watch the new release before declaring it good. Long enough to
# catch a service that crash-loops on a timer rather than immediately.
SOAK_SECONDS="${ADIONA_SOAK_SECONDS:-90}"
SOAK_POLL="${ADIONA_SOAK_POLL:-5}"
CONTROLLER_WAIT="${ADIONA_CONTROLLER_WAIT:-30}"
KEEP_RELEASES="${ADIONA_KEEP_RELEASES:-3}"

log() { echo "[adiona-apply] $*"; }
die() { log "FATAL: $*"; exit 1; }

phase() {
	mkdir -p "$RUN_DIR" 2>/dev/null || true
	printf '%s\n' "$1" > "$PHASE_FILE" 2>/dev/null || true
	log "phase: $1"
}

# ── Relocate out of the release tree ─────────────────────────────────────────
# Both --migrate and the apply path delete or repoint the directory this script
# lives in. The open inode would survive, but bash reads the script lazily and any
# sibling file read would break, so re-exec from a copy outside the tree first.
#
# The copy goes in STATE_DIR (/var/lib/adiona), NOT /run. /run is tmpfs and looks
# ideal - cleared at boot, so nothing stale accumulates - but on this image it is
# mounted `noexec`:
#
#     tmpfs /run tmpfs rw,nosuid,nodev,noexec,relatime,...
#
# so exec'ing from there fails with "Permission denied" even as root with mode
# 755, which reads like a file-permission problem and is not one. The apply died
# on that line, before logging anything, leaving the screen on "Installing
# update" with nothing in the applier's journal to explain it.
#
# STATE_DIR is on the card and therefore persistent, which is fine: the copy is
# overwritten from the outgoing release on every run, so it is never stale.
RELOC="$STATE_DIR/apply.sh"
if [ -z "${ADIONA_APPLY_RELOCATED:-}" ]; then
	self="$(readlink -f "$0")"
	case "$self" in
		"$ROOT"/*)
			mkdir -p "$STATE_DIR"
			cp -f "$self" "$RELOC"
			chmod +x "$RELOC"
			# Prove the destination is actually exec-capable before relying on it.
			# A noexec mount here is otherwise a bare "Permission denied" at a line
			# number, with no hint that the filesystem is the problem.
			if ! "$RELOC" --exec-probe >/dev/null 2>&1; then
				log "FATAL: cannot execute $RELOC - is its filesystem mounted noexec?"
				log "       $(grep -E \" $(df --output=target "$STATE_DIR" | tail -1) \" /proc/mounts 2>/dev/null | head -1)"
				exit 1
			fi
			export ADIONA_APPLY_RELOCATED=1
			exec "$RELOC" "$@"
			;;
	esac
	export ADIONA_APPLY_RELOCATED=1
fi

# Answer the probe above and exit. Must come after the relocation block (so the
# probe runs against the COPY) and before anything that touches the system.
case "${1:-}" in --exec-probe) exit 0 ;; esac

# ── Durability helpers ───────────────────────────────────────────────────────
# bash cannot fsync a directory; python3 can, and it is already a hard dependency
# (it runs the controller, the wheel service and the updater).
fsync_path() {
	python3 - "$1" <<'PY' 2>/dev/null || true
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
}

write_marker() {
	# $1 op, $2 from, $3 to, $4 previous release name
	mkdir -p "$ETC"
	{
		echo "op=$1"
		echo "from=$2"
		echo "to=$3"
		echo "prev=$4"
		echo "at=$(date -u +%s)"
	} > "$MARKER"
	fsync_path "$MARKER"
	fsync_path "$ETC"          # ordering, not just durability — see the header
}

clear_marker() {
	rm -f "$MARKER"
	fsync_path "$ETC"
}

marker_get() {
	[ -f "$MARKER" ] || return 1
	sed -n "s/^$1=//p" "$MARKER" | head -1
}

write_result() {
	# $1 op, $2 from, $3 to, $4 ok(true|false), $5 reason
	mkdir -p "$STATE_DIR"
	local tmp="$RESULT.tmp"
	printf '{"op":"%s","from":"%s","to":"%s","ok":%s,"reason":"%s","at":%s}\n' \
		"$1" "$2" "$3" "$4" "$(printf '%s' "$5" | tr -d '"\\')" "$(date -u +%s)" > "$tmp"
	mv -f "$tmp" "$RESULT"
	fsync_path "$STATE_DIR"
}

# ── Layout ───────────────────────────────────────────────────────────────────

current_release() {
	if [ -L "$CURRENT" ]; then
		basename "$(readlink "$CURRENT")"
	else
		echo ""
	fi
}

installed_version() {
	cat "$ROOT/VERSION" 2>/dev/null | head -1 | tr -d '[:space:]'
}

# Atomic: one rename(2) of a symlink over a symlink. `ln -sfn X current` on its
# own is NOT atomic — coreutils does unlink() then symlink(), and a power cut
# between them leaves no `current` at all.
point_current_at() {
	local rel="$1"
	[ -d "$RELEASES/$rel" ] || die "release $rel does not exist"
	ln -sfn "releases/$rel" "$ROOT/.current.tmp"
	mv -T "$ROOT/.current.tmp" "$CURRENT"
	fsync_path "$ROOT"
	log "current -> releases/$rel"
}

# Idempotent and resumable. Safe to call on a fully migrated box (no-op), on a
# flat box (full migration), and on a box interrupted half way (repair).
repair_layout() {
	local ver rel name
	ver="$(current_release)"
	if [ -z "$ver" ]; then
		ver="$(installed_version)"
		[ -n "$ver" ] || die "cannot determine the installed version (no $ROOT/VERSION)"
	fi
	rel="$RELEASES/$ver"

	# 1. Populate the release directory from whatever real content is still at the
	#    top level. Copy-first: nothing is removed until the copy exists.
	mkdir -p "$rel"
	for name in $LINKED; do
		if [ ! -e "$rel/$name" ] && [ -e "$ROOT/$name" ] && [ ! -L "$ROOT/$name" ]; then
			cp -a "$ROOT/$name" "$rel/$name"
			log "staged $name into releases/$ver"
		fi
	done
	# The live box.conf is the only template a migrated release can honestly claim
	# to have shipped with; a later OTA brings its own.
	if [ ! -f "$rel/config/box.conf" ] && [ -f "$ETC/box.conf" ]; then
		mkdir -p "$rel/config"
		cp -a "$ETC/box.conf" "$rel/config/box.conf"
	fi
	backup_installed_units "$ver"
	sync
	fsync_path "$rel"

	# 2. current -> this release.
	if [ "$(current_release)" != "$ver" ]; then
		point_current_at "$ver"
	fi

	# 3. Replace each real top-level name with a symlink. Destructive-last, one
	#    name at a time, and only when the copy under releases/ is already there —
	#    so the worst a power cut can do is leave one name missing, which the next
	#    --boot-check restores from the same place.
	for name in $LINKED; do
		if [ -L "$ROOT/$name" ]; then
			continue
		fi
		if [ ! -e "$rel/$name" ]; then
			log "WARNING: releases/$ver/$name missing; leaving $ROOT/$name as-is"
			continue
		fi
		rm -rf "$ROOT/$name"
		ln -s "current/$name" "$ROOT/$name"
		log "linked $name -> current/$name"
	done
	fsync_path "$ROOT"
}

backup_installed_units() {
	local rel="$RELEASES/$1"
	local dest="$rel/units-installed"
	mkdir -p "$dest"
	local u found=0
	for u in "$SYSTEMD_DIR"/adiona-*.service; do
		[ -f "$u" ] || continue
		cp -a "$u" "$dest/"
		found=1
	done
	[ "$found" = 1 ] || log "WARNING: no adiona-*.service found to back up"
}

# Rolling back past a release that ADDED a unit would otherwise leave a dangling
# symlink in multi-user.target.wants/ and a systemd error on every boot.
disable_added_units() {
	local failed="$1" prev="$2"
	local src="$RELEASES/$failed/units" prev_dir="$RELEASES/$prev/units-installed"
	[ -d "$src" ] || return 0
	local u name changed=0
	for u in "$src"/*.service; do
		[ -f "$u" ] || continue
		name="$(basename "$u")"
		[ -f "$prev_dir/$name" ] && continue
		log "disabling $name — added by $failed, unknown to $prev"
		systemctl disable "$name" >/dev/null 2>&1 || true
		rm -f "$SYSTEMD_DIR/$name"
		changed=1
	done
	[ "$changed" = 1 ] && systemctl daemon-reload
	return 0
}

restore_installed_units() {
	local dest="$RELEASES/$1/units-installed"
	if [ ! -d "$dest" ]; then
		log "WARNING: no unit backup for $1 — leaving the installed units alone"
		return 0
	fi
	local u
	for u in "$dest"/*.service; do
		[ -f "$u" ] || continue
		install -m 0644 "$u" "$SYSTEMD_DIR/"
	done
	systemctl daemon-reload
	log "restored unit files from releases/$1/units-installed"
}

install_release_units() {
	local src="$RELEASES/$1/units"
	[ -d "$src" ] || { log "release $1 ships no unit files"; return 0; }
	local u
	for u in "$src"/*.service; do
		[ -f "$u" ] || continue
		install -m 0644 "$u" "$SYSTEMD_DIR/"
	done
	systemctl daemon-reload
	# Enable here as well as in the image stage: a box flashed before a unit
	# existed has no enablement for it, and an OTA is the only way it will ever
	# get one short of a reflash.
	for u in "$src"/*.service; do
		[ -f "$u" ] || continue
		systemctl enable "$(basename "$u")" >/dev/null 2>&1 || true
	done
	log "installed unit files from releases/$1/units"
}

# ── Health ───────────────────────────────────────────────────────────────────

controller_answers() {
	curl -sf --max-time 2 "$STATE_URL" >/dev/null 2>&1
}

wait_for_controller() {
	local i
	for i in $(seq 1 "$CONTROLLER_WAIT"); do
		controller_answers && return 0
		sleep 1
	done
	return 1
}

nrestarts() {
	systemctl show -p NRestarts --value "$1" 2>/dev/null || echo 0
}

# Seconds since the kiosk page last fetched /state. This is the ONLY signal that
# distinguishes "adiona-kiosk is active" from "Chromium is sitting on its
# connection-error page": the error page never polls, but the unit is perfectly
# active and `systemctl is-active` says everything is fine.
ui_seen_ago() {
	curl -sf --max-time 2 "$STATE_URL" 2>/dev/null \
		| sed -n 's/.*"ui_seen_ago"[[:space:]]*:[[:space:]]*\([0-9.]*\).*/\1/p' | head -1
}

# $1 = "expect-ui" to also require a live kiosk page.
health_check() {
	local want_ui="${1:-}"
	local u seen
	for u in $UNITS; do
		if [ "$(systemctl is-active "$u")" != "active" ]; then
			echo "$u is $(systemctl is-active "$u")"
			return 1
		fi
	done
	if ! controller_answers; then
		echo "controller does not answer $STATE_URL"
		return 1
	fi
	if [ "$want_ui" = "expect-ui" ]; then
		seen="$(ui_seen_ago)"
		if [ -z "$seen" ]; then
			echo "controller reports no ui_seen_ago"
			return 1
		fi
		# 20 s is generous: the page polls at 1 Hz, and a kiosk restart takes
		# 5-15 s of black screen before the new page's first fetch lands.
		if ! awk -v s="$seen" -v lim=20 'BEGIN { exit !(s + 0 < lim) }'; then
			echo "kiosk page has not polled for ${seen}s (Chromium error page?)"
			return 1
		fi
	fi
	return 0
}

# Watch for a crash-loop. `is-active` alone lies about a service that restarts
# every 2 s under Restart=always: sampled between restarts it reads "active".
soak() {
	local want_ui="$1"; shift
	local baseline="$1"; shift          # "unit:count unit:count ..."
	local deadline=$(( $(date +%s) + SOAK_SECONDS ))
	local reason u before now
	while [ "$(date +%s)" -lt "$deadline" ]; do
		if ! reason="$(health_check "$want_ui")"; then
			echo "$reason"
			return 1
		fi
		for u in $UNITS; do
			before="$(echo "$baseline" | tr ' ' '\n' | sed -n "s/^$u://p")"
			now="$(nrestarts "$u")"
			if [ "${now:-0}" -gt "$(( ${before:-0} + 1 ))" ]; then
				echo "$u is crash-looping (${before} -> ${now} restarts)"
				return 1
			fi
		done
		sleep "$SOAK_POLL"
	done
	return 0
}

capture_restarts() {
	local u out=""
	for u in $UNITS; do
		out="$out $u:$(nrestarts "$u")"
	done
	echo "${out# }"
}

# ── Release housekeeping ─────────────────────────────────────────────────────

# Content fingerprint of a release subtree, so an update that does not touch the
# kiosk does not have to black the screen out to install. __pycache__ is excluded
# because the running services write it into whichever release they run from.
tree_hash() {
	[ -d "$1" ] || { echo "absent"; return 0; }
	( cd "$1" && find . -type f ! -name '*.pyc' -print0 \
		| LC_ALL=C sort -z \
		| xargs -0 -r sha256sum 2>/dev/null \
		| sha256sum ) | cut -d' ' -f1
}

# Keep KEEP_RELEASES directories in total. The current and previous releases are
# always two of them however old they are — the previous one is the rollback
# target, so pruning it would strip the box of its way back — and the remaining
# budget goes to the newest of the rest. The two protected slots are reserved up
# front rather than counted as they are met, because the previous release is
# usually the OLDEST directory: counting in mtime order would spend the whole
# budget on newer ones and then keep the rollback target on top of it.
gc_releases() {
	local keep_a="$1" keep_b="$2"
	local extra=$(( KEEP_RELEASES - 2 ))
	[ "$extra" -lt 0 ] && extra=0
	local kept=0 d name
	for d in $(ls -1dt "$RELEASES"/*/ 2>/dev/null); do
		name="$(basename "$d")"
		if [ "$name" = "$keep_a" ] || [ "$name" = "$keep_b" ]; then
			continue
		fi
		if [ "$kept" -lt "$extra" ]; then
			kept=$(( kept + 1 ))
			continue
		fi
		log "pruning old release $name"
		rm -rf "$d"
	done
}

# ── Operations ───────────────────────────────────────────────────────────────

do_apply() {
	local from="$1" to="$2" reldir="$3"
	local prev restarts reason restart_kiosk

	[ -d "$reldir" ] || die "release directory $reldir does not exist"
	local name
	for name in web controller kiosk wheel first-boot updater VERSION; do
		[ -e "$reldir/$name" ] || die "release $to is incomplete: missing $name"
	done

	phase "preparing"
	repair_layout                      # a never-migrated box migrates here, once
	prev="$(current_release)"
	[ -n "$prev" ] || die "layout repair did not produce a current release"
	if [ "$prev" = "$to" ]; then
		log "already on $to — nothing to do"
		write_result apply "$from" "$to" true "already current"
		return 0
	fi

	# Packages BEFORE the swap, so an apt failure leaves the box entirely
	# untouched. Only what is actually missing gets installed, matching
	# deploy.ps1 --packages.
	if [ -s "$reldir/packages" ]; then
		phase "packages"
		install_packages "$reldir/packages" || {
			write_result apply "$from" "$to" false "required packages could not be installed"
			apt-get clean || true
			return 1
		}
	fi

	# box.conf merge, using the INCOMING release's merger so a release can improve
	# it. If it fails we abort here, before anything has been swapped.
	if [ -f "$reldir/config/box.conf" ] && [ -x "$reldir/updater/adiona-updater.py" ]; then
		phase "config"
		python3 "$reldir/updater/adiona-updater.py" --merge-conf "$reldir/config/box.conf" || {
			write_result apply "$from" "$to" false "box.conf merge failed"
			return 1
		}
	fi

	backup_installed_units "$prev"
	restart_kiosk=no
	if [ "$(tree_hash "$RELEASES/$prev/kiosk")" != "$(tree_hash "$reldir/kiosk")" ]; then
		restart_kiosk=yes
		log "kiosk subtree changed — the display will restart"
	else
		log "kiosk subtree unchanged — the page will reload in place"
	fi

	# Give the UI a moment to paint "installing, do not remove power" before the
	# screen goes anywhere. Without that, an operator sees an unexplained black
	# screen and pulls the plug — the exact failure this whole mechanism exists to
	# survive.
	phase "applying"
	sleep 2

	write_marker apply "$prev" "$to" "$prev"
	point_current_at "$to"
	install_release_units "$to"

	# Controller first, and wait for it: kiosk-session.sh gives up waiting after
	# 30 s and starts Chromium anyway, which then loads a connection error and
	# never reloads.
	restarts="$(capture_restarts)"
	systemctl restart adiona-controller || true
	if ! wait_for_controller; then
		reason="controller did not answer within ${CONTROLLER_WAIT}s"
		rollback_to "$prev" "$to" "$reason"
		return 1
	fi

	systemctl restart adiona-wheel || true
	systemctl restart adiona-updater || true
	if [ "$restart_kiosk" = yes ]; then
		systemctl restart adiona-kiosk || true
	fi

	# expect-ui either way: whether or not the kiosk unit was restarted, a healthy
	# box has a page polling /state, and its absence is the one failure that
	# `systemctl is-active` cannot see.
	phase "health"
	if ! reason="$(soak expect-ui "$restarts")"; then
		rollback_to "$prev" "$to" "$reason"
		return 1
	fi

	clear_marker
	printf 'v%s applied over v%s at %s\n' "$to" "$prev" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" \
		> "$UPDATED_STAMP"
	gc_releases "$to" "$prev"
	write_result apply "$prev" "$to" true ""
	phase "ok"
	log "updated $prev -> $to"
}

install_packages() {
	local list="$1" pkg missing=()
	while read -r pkg; do
		[ -n "$pkg" ] || continue
		case "$pkg" in \#*) continue ;; esac
		dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed" || missing+=("$pkg")
	done < "$list"
	if [ ${#missing[@]} -eq 0 ]; then
		log "packages: all present"
		return 0
	fi
	log "packages: installing ${missing[*]}"
	apt-get update -qq || return 1
	# Download first so the install phase is short and offline: a hotspot dropping
	# mid-download must not leave dpkg half configured.
	apt-get -y -qq -d install "${missing[@]}" || return 1
	DEBIAN_FRONTEND=noninteractive apt-get -y -qq --no-install-recommends \
		-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
		install "${missing[@]}" || return 1
	apt-get clean || true
}

rollback_to() {
	local prev="$1" failed="$2" reason="$3"
	phase "rolling back"
	log "ROLLBACK: $reason"
	point_current_at "$prev"
	disable_added_units "$failed" "$prev"
	restore_installed_units "$prev"
	systemctl restart adiona-controller || true
	wait_for_controller || log "WARNING: controller still not answering after rollback"
	systemctl restart adiona-wheel || true
	systemctl restart adiona-updater || true
	systemctl restart adiona-kiosk || true
	clear_marker
	write_result apply "$prev" "$failed" false "$reason"
	phase "failed"
	log "rolled back to $prev"
}

# Runs from adiona-rollback.service, before any adiona service starts.
do_boot_check() {
	local op from to prev

	# A power cut mid-dpkg is reachable now that we install packages, and a
	# half-configured dpkg breaks every later apt operation until it is cleared.
	if [ -n "$(dpkg --audit 2>/dev/null)" ]; then
		log "dpkg reports an interrupted install — configuring"
		DEBIAN_FRONTEND=noninteractive dpkg --configure -a || true
	fi

	if [ ! -f "$MARKER" ]; then
		log "no update marker — nothing to do"
		return 0
	fi

	op="$(marker_get op || echo apply)"
	from="$(marker_get from || echo '')"
	to="$(marker_get to || echo '')"
	prev="$(marker_get prev || echo '')"
	log "marker found: op=$op from=$from to=$to prev=$prev"

	if [ "$op" = "migrate" ]; then
		# Nothing to undo — the release directory is a faithful copy of what was
		# already running. Just finish the job.
		repair_layout
		clear_marker
		write_result migrate "$from" "$to" true "completed at boot"
		return 0
	fi

	# Roll back on the FIRST boot that finds the marker. Do not count attempts:
	# "power cut during apply" and "new code wedged the box so the operator pulled
	# the plug" are indistinguishable, and an unnecessary rollback costs one
	# re-download while a missed one costs a dead box at a customer site.
	repair_layout
	if [ -n "$prev" ] && [ -d "$RELEASES/$prev" ]; then
		point_current_at "$prev"
		[ -n "$to" ] && disable_added_units "$to" "$prev"
		restore_installed_units "$prev"
		write_result apply "$prev" "$to" false "interrupted; rolled back at boot"
		log "rolled back to $prev after an interrupted update"
	else
		write_result apply "$from" "$to" false "interrupted; no previous release to roll back to"
		log "WARNING: no previous release $prev to roll back to — continuing on $(current_release)"
	fi
	clear_marker
}

do_migrate() {
	local ver
	ver="$(current_release)"
	if [ -n "$ver" ] && [ -L "$ROOT/web" ] && [ -L "$ROOT/VERSION" ]; then
		log "already on the release layout (current -> $ver)"
		return 0
	fi
	ver="$(installed_version)"
	# The marker makes an interrupted migration self-repairing: the box reboots,
	# adiona-rollback.service sees it, and --boot-check finishes the job.
	write_marker migrate "$ver" "$ver" ""
	repair_layout
	clear_marker
	write_result migrate "$ver" "$ver" true ""
	log "migrated to the release layout (current -> $(current_release))"
}

do_manual_rollback() {
	local cur prev d
	cur="$(current_release)"
	[ -n "$cur" ] || die "not on the release layout — nothing to roll back"
	prev=""
	for d in $(ls -1dt "$RELEASES"/*/ 2>/dev/null); do
		[ "$(basename "$d")" = "$cur" ] && continue
		prev="$(basename "$d")"
		break
	done
	[ -n "$prev" ] || die "no other release to roll back to"
	log "manual rollback: $cur -> $prev"
	rollback_to "$prev" "$cur" "manual rollback requested"
}

# ── Argument handling ────────────────────────────────────────────────────────

main() {
	local mode="" protocol=1 from="" to="" reldir=""
	while [ $# -gt 0 ]; do
		case "$1" in
			--protocol)    protocol="$2"; shift ;;
			--from)        from="$2"; shift ;;
			--to)          to="$2"; shift ;;
			--release-dir) reldir="$2"; shift ;;
			--boot-check)  mode=boot ;;
			--migrate)     mode=migrate ;;
			--rollback)    mode=rollback ;;
			-h|--help)     sed -n '2,50p' "$0"; exit 0 ;;
			*)             die "unknown argument: $1" ;;
		esac
		shift
	done

	# ADIONA_ROOT set means a sandbox run (see the path block above), which owns
	# nothing privileged and must not demand root.
	[ "$(id -u)" = 0 ] || [ -n "${ADIONA_ROOT:-}" ] \
		|| die "must run as root"
	mkdir -p "$RELEASES" "$STATE_DIR" "$RUN_DIR"

	case "$mode" in
		boot)     do_boot_check; return ;;
		migrate)  do_migrate; return ;;
		rollback) : ;;
		*)        [ -n "$to" ] && [ -n "$reldir" ] || die "need --to and --release-dir (or --boot-check/--migrate/--rollback)" ;;
	esac

	[ "$protocol" = 1 ] || die "unsupported apply protocol '$protocol' — this box is too old for that release"

	if [ "$mode" = rollback ]; then
		do_manual_rollback
		return
	fi
	do_apply "$from" "$to" "$reldir"
}

# --help costs nothing and must not require root.
case " $* " in
	*" --help "*|*" -h "*) sed -n '2,60p' "$0"; exit 0 ;;
esac

# Root is checked HERE rather than only inside main(), because the lock below is
# taken by a shell redirection into a root-owned directory: an unprivileged run
# would die with a bare "Permission denied" pointing at a line number, instead of
# saying the one thing that would fix it.
if [ "$(id -u)" != 0 ] && [ -z "${ADIONA_ROOT:-}" ]; then
	log "FATAL: must run as root — try: sudo $0 $*"
	exit 1
fi

# One applier at a time, and deploy.ps1's install.sh takes the same lock — two
# concurrent writers to /opt/adiona produce an undefined tree.
exec 9>"$LOCK"
if ! flock -n 9; then
	die "another update or deploy is in progress (lock $LOCK)"
fi

main "$@"
