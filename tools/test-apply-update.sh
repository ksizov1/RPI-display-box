#!/usr/bin/env bash
#
# Sandbox test for system/updater/apply-update.sh.
#
# The apply script is the only thing on the box allowed to change which release is
# running, and its two most important paths — rollback after a bad release, and
# repair after a power cut mid-update — are exactly the ones you cannot rehearse
# on a live box at an event. So every path is exercised here instead, against a
# throwaway tree under /tmp, using the ADIONA_ROOT / ADIONA_ETC / ADIONA_SYSTEMD_DIR
# overrides the script accepts for that purpose. Nothing outside the sandbox is
# touched and nothing needs root.
#
# systemctl and curl are stubbed with files a test can rewrite mid-run; that is
# how the crash-loop and the "Chromium is sitting on its error page" cases are
# simulated, since neither is visible to `systemctl is-active`.
#
# Usage — needs real symlinks, so Linux or WSL, NOT Git Bash on Windows:
#     bash tools/test-apply-update.sh
#     wsl bash tools/test-apply-update.sh      # from a Windows checkout
#
# Exits non-zero if any assertion fails.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${1:-$HERE/../system/updater/apply-update.sh}"
[ -f "$SCRIPT" ] || { echo "cannot find apply-update.sh at $SCRIPT" >&2; exit 1; }

probe="/tmp/.adiona-symlink-probe.$$"
if ! ln -s /tmp "$probe" 2>/dev/null; then
	echo "this filesystem cannot create symlinks — run under Linux or WSL" >&2
	exit 1
fi
rm -f "$probe"

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

export ADIONA_ROOT="$SANDBOX/opt/adiona"
export ADIONA_ETC="$SANDBOX/etc/adiona"
export ADIONA_STATE_DIR="$SANDBOX/var/lib/adiona"
export ADIONA_RUN_DIR="$SANDBOX/run/adiona"
export ADIONA_SYSTEMD_DIR="$SANDBOX/etc/systemd/system"
export ADIONA_SOAK_SECONDS=4
export ADIONA_SOAK_POLL=1
export ADIONA_CONTROLLER_WAIT=3
export PATH="$SANDBOX/bin:$PATH"

pass=0; fail=0
ok()    { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
bad()   { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1  [$2]"; fi; }
run()   { bash "$ADIONA_ROOT/updater/apply-update.sh" "$@"; }

# ── Stubs ────────────────────────────────────────────────────────────────────
# Driven by files rather than baked in, so a test can change what the box "is"
# while an apply is mid-flight.
mk_stubs() {
	mkdir -p "$SANDBOX/bin"
	echo active > "$SANDBOX/ctl.active"
	echo 0      > "$SANDBOX/ctl.restarts"
	echo 0.4    > "$SANDBOX/ui.seen"
	echo yes    > "$SANDBOX/ctl.answers"
	cat > "$SANDBOX/bin/systemctl" <<EOF
#!/bin/sh
case "\$1" in
  is-active) cat "$SANDBOX/ctl.active" ;;
  show)      cat "$SANDBOX/ctl.restarts" ;;
  restart)   echo "restart \$2" >> "$SANDBOX/restarts.log" ;;
esac
exit 0
EOF
	cat > "$SANDBOX/bin/curl" <<EOF
#!/bin/sh
[ "\$(cat "$SANDBOX/ctl.answers")" = yes ] || exit 7
printf '{"mode":"waiting","version":"x","ui_seen_ago":%s}' "\$(cat "$SANDBOX/ui.seen")"
EOF
	printf '#!/bin/sh\nexit 0\n' > "$SANDBOX/bin/dpkg"
	chmod +x "$SANDBOX/bin/systemctl" "$SANDBOX/bin/curl" "$SANDBOX/bin/dpkg"
}

# A box exactly as the image build leaves it: flat /opt/adiona, no releases/.
build_flat() {
	rm -rf "$SANDBOX"/opt "$SANDBOX"/etc "$SANDBOX"/var "$SANDBOX"/run "$SANDBOX/restarts.log"
	mkdir -p "$ADIONA_ROOT" "$ADIONA_ETC" "$ADIONA_SYSTEMD_DIR"
	local d u
	for d in web controller kiosk wheel first-boot updater; do
		mkdir -p "$ADIONA_ROOT/$d"
		echo "1.5.0 content of $d" > "$ADIONA_ROOT/$d/file.txt"
	done
	cp "$SCRIPT" "$ADIONA_ROOT/updater/apply-update.sh"
	chmod +x "$ADIONA_ROOT/updater/apply-update.sh"
	echo "1.5.0" > "$ADIONA_ROOT/VERSION"
	echo 'AP_GATEWAY="192.168.50.1"' > "$ADIONA_ETC/box.conf"
	for u in controller kiosk wheel updater; do
		printf '[Unit]\nDescription=adiona-%s v1.5.0\n' "$u" > "$ADIONA_SYSTEMD_DIR/adiona-$u.service"
	done
}

# A box already on the release layout.
build_migrated() {
	build_flat
	run --migrate >/dev/null 2>&1
	: > "$SANDBOX/restarts.log"
}

# Stage a candidate release. $2 = "same-kiosk" to reuse 1.5.0's kiosk verbatim,
# which is what makes the reload-in-place path testable.
stage() {
	local v="$1" mode="${2:-}" d
	local rel="$ADIONA_ROOT/releases/$v"
	mkdir -p "$rel/units"
	for d in web controller wheel first-boot updater; do
		mkdir -p "$rel/$d"; echo "$v $d" > "$rel/$d/file.txt"
	done
	if [ "$mode" = same-kiosk ]; then
		cp -a "$ADIONA_ROOT/releases/1.5.0/kiosk" "$rel/kiosk"
	else
		mkdir -p "$rel/kiosk"; echo "$v kiosk" > "$rel/kiosk/file.txt"
	fi
	cp "$SCRIPT" "$rel/updater/apply-update.sh"; chmod +x "$rel/updater/apply-update.sh"
	echo "$v" > "$rel/VERSION"
	for d in controller kiosk wheel updater; do
		printf '[Unit]\nDescription=adiona-%s v%s\n' "$d" "$v" > "$rel/units/adiona-$d.service"
	done
}

mk_stubs

# ═══ Layout, migration and crash repair ══════════════════════════════════════

echo "=== 1. flat -> release migration ==="
build_flat
run --migrate > "$SANDBOX/log" 2>&1 || bad "migrate exited $?"
check "current is a symlink"              '[ -L "$ADIONA_ROOT/current" ]'
check "current -> releases/1.5.0"         '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'
for d in web controller kiosk wheel first-boot updater VERSION; do
	check "$d is a symlink"               "[ -L \"\$ADIONA_ROOT/$d\" ]"
	check "$d still resolves"             "[ -e \"\$ADIONA_ROOT/$d\" ]"
done
check "VERSION still reads 1.5.0"         '[ "$(cat "$ADIONA_ROOT/VERSION")" = "1.5.0" ]'
check "content preserved"                 'grep -q "1.5.0 content of web" "$ADIONA_ROOT/web/file.txt"'
check "installed units backed up"         '[ -f "$ADIONA_ROOT/releases/1.5.0/units-installed/adiona-controller.service" ]'
check "box.conf captured as the template" '[ -f "$ADIONA_ROOT/releases/1.5.0/config/box.conf" ]'
check "marker cleared"                    '[ ! -f "$ADIONA_ETC/update-pending" ]'

echo "=== 2. migration is idempotent ==="
run --migrate > "$SANDBOX/log" 2>&1 || bad "second migrate exited $?"
check "current unchanged"                 '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'
check "web still a symlink"               '[ -L "$ADIONA_ROOT/web" ]'
check "recognised as already migrated"    'grep -q "already on the release layout" "$SANDBOX/log"'

echo "=== 3. power cut mid-migration -> boot-check repairs ==="
build_flat
# Release dir fully populated and `current` created, but only 2 of 7 names linked
# — precisely what write_marker + an interrupted repair_layout leaves behind.
REL="$ADIONA_ROOT/releases/1.5.0"
mkdir -p "$REL/units-installed" "$REL/config"
for d in web controller kiosk wheel first-boot updater; do cp -a "$ADIONA_ROOT/$d" "$REL/$d"; done
cp -a "$ADIONA_ROOT/VERSION" "$REL/VERSION"
cp -a "$ADIONA_ETC/box.conf" "$REL/config/box.conf"
cp -a "$ADIONA_SYSTEMD_DIR"/*.service "$REL/units-installed/"
ln -sfn "releases/1.5.0" "$ADIONA_ROOT/current"
for d in web controller; do rm -rf "$ADIONA_ROOT/$d"; ln -s "current/$d" "$ADIONA_ROOT/$d"; done
printf 'op=migrate\nfrom=1.5.0\nto=1.5.0\nprev=\nat=1\n' > "$ADIONA_ETC/update-pending"
check "precondition: kiosk is a real dir" '[ -d "$ADIONA_ROOT/kiosk" ] && [ ! -L "$ADIONA_ROOT/kiosk" ]'
bash "$REL/updater/apply-update.sh" --boot-check > "$SANDBOX/log" 2>&1 || bad "boot-check exited $?"
for d in web controller kiosk wheel first-boot updater VERSION; do
	check "repaired: $d is a symlink"     "[ -L \"\$ADIONA_ROOT/$d\" ]"
done
check "marker cleared"                    '[ ! -f "$ADIONA_ETC/update-pending" ]'
check "content survived the repair"       'grep -q "1.5.0 content of kiosk" "$ADIONA_ROOT/kiosk/file.txt"'

echo "=== 4. power cut mid-apply -> boot-check rolls back ==="
NEW="$ADIONA_ROOT/releases/1.6.0"
stage 1.6.0
# 1.6.0 also ships a unit 1.5.0 never had, which rollback must remove or systemd
# logs a dangling-symlink error on every subsequent boot.
printf '[Unit]\nDescription=adiona-newthing v1.6.0\n' > "$NEW/units/adiona-newthing.service"
cp "$NEW/units/adiona-newthing.service" "$ADIONA_SYSTEMD_DIR/"
ln -sfn "releases/1.6.0" "$ADIONA_ROOT/.ctmp" && mv -T "$ADIONA_ROOT/.ctmp" "$ADIONA_ROOT/current"
printf 'op=apply\nfrom=1.5.0\nto=1.6.0\nprev=1.5.0\nat=1\n' > "$ADIONA_ETC/update-pending"
check "precondition: current -> 1.6.0"    '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.6.0" ]'
run --boot-check > "$SANDBOX/log" 2>&1 || bad "boot-check exited $?"
check "rolled back to 1.5.0"              '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'
check "VERSION reads 1.5.0 again"         '[ "$(cat "$ADIONA_ROOT/VERSION")" = "1.5.0" ]'
check "web serves 1.5.0 content"          'grep -q "1.5.0 content of web" "$ADIONA_ROOT/web/file.txt"'
check "failed release kept for diagnosis" '[ -d "$ADIONA_ROOT/releases/1.6.0" ]'
check "unit added by 1.6.0 removed"       '[ ! -f "$ADIONA_SYSTEMD_DIR/adiona-newthing.service" ]'
check "1.5.0 units restored"              'grep -q "v1.5.0" "$ADIONA_SYSTEMD_DIR/adiona-controller.service"'
check "marker cleared"                    '[ ! -f "$ADIONA_ETC/update-pending" ]'
check "result records the failure"        'grep -q "\"ok\":false" "$ADIONA_STATE_DIR/last-apply.json"'

echo "=== 5. boot-check without a marker is a no-op ==="
before="$(readlink "$ADIONA_ROOT/current")"
run --boot-check > "$SANDBOX/log" 2>&1 || bad "boot-check exited $?"
check "current unchanged"                 '[ "$(readlink "$ADIONA_ROOT/current")" = "$before" ]'
check "reported nothing to do"            'grep -q "no update marker" "$SANDBOX/log"'

echo "=== 6. a concurrent apply or deploy is refused ==="
( exec 9>"$ADIONA_ROOT/.update.lock"; flock 9; sleep 3 ) &
holder=$!
sleep 0.4
out="$(run --boot-check 2>&1)"; rc=$?
check "second run failed"                 "[ $rc -ne 0 ]"
check "reported the lock"                 'echo "$out" | grep -q "another update or deploy is in progress"'
wait $holder 2>/dev/null

# ═══ The apply path ══════════════════════════════════════════════════════════

echo "=== 7. successful apply ==="
build_migrated; stage 1.6.0
run --protocol 1 --from 1.5.0 --to 1.6.0 --release-dir "$ADIONA_ROOT/releases/1.6.0" \
    > "$SANDBOX/log" 2>&1 || bad "apply exited $?"
check "current -> 1.6.0"                  '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.6.0" ]'
check "VERSION reads 1.6.0"               '[ "$(cat "$ADIONA_ROOT/VERSION")" = "1.6.0" ]'
check "web serves 1.6.0"                  'grep -q "1.6.0 web" "$ADIONA_ROOT/web/file.txt"'
check "new units installed"               'grep -q "v1.6.0" "$ADIONA_SYSTEMD_DIR/adiona-controller.service"'
check "outgoing units backed up"          'grep -q "v1.5.0" "$ADIONA_ROOT/releases/1.5.0/units-installed/adiona-controller.service"'
check "marker cleared"                    '[ ! -f "$ADIONA_ETC/update-pending" ]'
check "result ok"                         'grep -q "\"ok\":true" "$ADIONA_STATE_DIR/last-apply.json"'
check ".updated stamp written"            'grep -q "v1.6.0 applied over v1.5.0" "$ADIONA_ETC/.updated"'
check "phase ended at ok"                 '[ "$(cat "$ADIONA_RUN_DIR/apply.phase")" = "ok" ]'
check "kiosk changed -> restarted"        'grep -q "restart adiona-kiosk" "$SANDBOX/restarts.log"'

echo "=== 8. kiosk subtree unchanged -> the display is not restarted ==="
build_migrated; stage 1.6.1 same-kiosk
run --protocol 1 --from 1.5.0 --to 1.6.1 --release-dir "$ADIONA_ROOT/releases/1.6.1" \
    > "$SANDBOX/log" 2>&1 || bad "apply exited $?"
check "applied"                           '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.6.1" ]'
check "controller restarted"              'grep -q "restart adiona-controller" "$SANDBOX/restarts.log"'
check "kiosk NOT restarted"               '! grep -q "restart adiona-kiosk" "$SANDBOX/restarts.log"'
check "logged reload-in-place"            'grep -q "kiosk subtree unchanged" "$SANDBOX/log"'

echo "=== 9. controller never answers -> rollback ==="
build_migrated; stage 1.7.0
echo no > "$SANDBOX/ctl.answers"
run --protocol 1 --from 1.5.0 --to 1.7.0 --release-dir "$ADIONA_ROOT/releases/1.7.0" \
    > "$SANDBOX/log" 2>&1
check "rolled back to 1.5.0"              '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'
check "VERSION back to 1.5.0"             '[ "$(cat "$ADIONA_ROOT/VERSION")" = "1.5.0" ]'
check "1.5.0 units restored"              'grep -q "v1.5.0" "$ADIONA_SYSTEMD_DIR/adiona-controller.service"'
check "marker cleared"                    '[ ! -f "$ADIONA_ETC/update-pending" ]'
check "result names the reason"           'grep -q "did not answer" "$ADIONA_STATE_DIR/last-apply.json"'
echo yes > "$SANDBOX/ctl.answers"

echo "=== 10. units active but the page is dead -> rollback ==="
# The case `systemctl is-active` cannot see: Chromium loaded a connection error
# page and will never reload, so nothing polls /state even though every unit is
# perfectly active.
build_migrated; stage 1.7.1
echo 999 > "$SANDBOX/ui.seen"
run --protocol 1 --from 1.5.0 --to 1.7.1 --release-dir "$ADIONA_ROOT/releases/1.7.1" \
    > "$SANDBOX/log" 2>&1
check "rolled back"                       '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'
check "blamed the kiosk page"             'grep -q "has not polled" "$SANDBOX/log"'
echo 0.4 > "$SANDBOX/ui.seen"

echo "=== 11. a service crash-loops -> rollback ==="
# Restart counts must climb AFTER the baseline is taken, which happens once the
# script's 2 s "do not remove power" pause has elapsed — hence 5 s into a 10 s soak.
build_migrated; stage 1.7.2
( sleep 5; echo 9 > "$SANDBOX/ctl.restarts" ) &
ADIONA_SOAK_SECONDS=10 run --protocol 1 --from 1.5.0 --to 1.7.2 \
    --release-dir "$ADIONA_ROOT/releases/1.7.2" > "$SANDBOX/log" 2>&1
wait
check "rolled back"                       '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'
check "blamed the crash-loop"             'grep -q "crash-looping" "$SANDBOX/log"'
echo 0 > "$SANDBOX/ctl.restarts"

echo "=== 12. an incomplete release is refused before anything changes ==="
build_migrated; stage 1.7.3; rm -rf "$ADIONA_ROOT/releases/1.7.3/controller"
run --protocol 1 --from 1.5.0 --to 1.7.3 --release-dir "$ADIONA_ROOT/releases/1.7.3" \
    > "$SANDBOX/log" 2>&1
check "refused"                           'grep -q "incomplete" "$SANDBOX/log"'
check "current untouched"                 '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'
check "no marker left behind"             '[ ! -f "$ADIONA_ETC/update-pending" ]'

echo "=== 13. an unsupported apply protocol is refused ==="
build_migrated; stage 1.7.4
run --protocol 9 --from 1.5.0 --to 1.7.4 --release-dir "$ADIONA_ROOT/releases/1.7.4" \
    > "$SANDBOX/log" 2>&1
check "refused"                           'grep -q "unsupported apply protocol" "$SANDBOX/log"'
check "current untouched"                 '[ "$(readlink "$ADIONA_ROOT/current")" = "releases/1.5.0" ]'

echo "=== 14. GC keeps the current and previous releases and prunes the rest ==="
build_migrated
for v in 1.6.0 1.6.1 1.6.2 1.6.3; do stage "$v"; done
ADIONA_KEEP_RELEASES=3 run --protocol 1 --from 1.5.0 --to 1.6.3 \
    --release-dir "$ADIONA_ROOT/releases/1.6.3" > "$SANDBOX/log" 2>&1 || bad "apply exited $?"
echo "     kept: $(ls -1 "$ADIONA_ROOT/releases" | tr '\n' ' ')"
check "current kept"                      'ls "$ADIONA_ROOT/releases" | grep -qx "1.6.3"'
check "rollback target kept"              'ls "$ADIONA_ROOT/releases" | grep -qx "1.5.0"'
check "pruned to KEEP_RELEASES"           '[ "$(ls -1 "$ADIONA_ROOT/releases" | wc -l)" -le 3 ]'

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
