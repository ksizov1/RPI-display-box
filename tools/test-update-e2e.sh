#!/usr/bin/env bash
#
# End-to-end test of the update path, in a sandbox: a signed manifest, a real
# tarball, the real updater downloading and staging it, and the real applier
# swapping and rolling back.
#
# Complements tools/test-apply-update.sh, which covers the applier's failure modes
# in isolation. This one covers the parts that only show up when the pieces are
# joined: does the updater accept a manifest signed the way the licence server
# signs one, does the tarball this repo builds unpack into a release directory the
# applier will accept, and does a manifest signed with the WRONG key get refused.
#
# That last check is the reason this exists. Signing is the only thing standing
# between "a box asks a server for software" and "whoever runs the venue's DNS
# owns the fleet", and it is not otherwise exercised until a real release ships.
#
# Needs: bash, python3, openssl, tar — and real symlinks, so Linux or WSL, NOT
# Git Bash on Windows. Nothing outside the sandbox is touched; no root needed.
#
#     bash tools/test-update-e2e.sh
#     wsl bash tools/test-update-e2e.sh        # from a Windows checkout

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${ADIONA_TEST_PORT:-8899}"
SB="$(mktemp -d)"
SRV_PID=""
cleanup() { [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; rm -rf "$SB"; }
trap cleanup EXIT

probe="/tmp/.adiona-symlink-probe.$$"
ln -s /tmp "$probe" 2>/dev/null || {
	echo "this filesystem cannot create symlinks — run under Linux or WSL" >&2; exit 1; }
rm -f "$probe"

pass=0; fail=0
ok()  { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }
chk() { if eval "$2"; then ok "$1"; else bad "$1  [$2]"; fi; }

cd "$REPO"

echo "=== 0. signing key and a release tarball ==="
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$SB/priv.pem" 2>/dev/null
openssl rsa -in "$SB/priv.pem" -pubout -out "$SB/pub.pem" 2>/dev/null
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$SB/evil.pem" 2>/dev/null
mkdir -p "$SB/dist" "$SB/src16"
cp -r web controller system config VERSION "$SB/src16/"
echo "1.6.0" > "$SB/src16/VERSION"
# A visible change OUTSIDE kiosk/, so the applier's "kiosk unchanged, just reload
# the page" fast path is the one under test — that is the common case in practice.
sed -i 's/Waiting for a headset/Waiting for a headset (v1.6.0)/' "$SB/src16/web/index.html"
( cd "$SB/src16" && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
    --exclude=__pycache__ -cf - web controller system config VERSION \
    | gzip -n -9 > "$SB/dist/adiona-tv-1.6.0.tar.gz" )
SHA="$(sha256sum "$SB/dist/adiona-tv-1.6.0.tar.gz" | cut -d' ' -f1)"
SIZE="$(wc -c < "$SB/dist/adiona-tv-1.6.0.tar.gz" | tr -d ' ')"
echo "  tarball: $SIZE bytes, sha256 ${SHA:0:16}…"

echo "=== 1. a manifest server, signing the way routes/boxUpdates.js does ==="
cat > "$SB/server.py" <<PYEOF
import base64, http.server, json, os, subprocess, sys
KEY = sys.argv[1]
MANIFEST = {"schema": 1, "channel": "stable", "min_client_protocol": 1, "pins": {},
  "releases": [{"version": "1.6.0",
    "url": "http://127.0.0.1:$PORT/adiona-tv-1.6.0.tar.gz",
    "size": $SIZE, "sha256": "$SHA", "notes": "End-to-end test release.",
    "min_from": "1.0.0", "min_image": "1.0.0", "packages": [], "rollout": 100}]}

def sign(obj):
    # Must match canonicalise() in routes/boxUpdates.js exactly: sorted keys, no
    # whitespace. To a box, a disagreement here is indistinguishable from forgery.
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", KEY],
                       input=canon, capture_output=True)
    return base64.b64encode(p.stdout).decode()

class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        body = json.dumps({"manifest": MANIFEST, "signature": sign(MANIFEST)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

os.chdir("$SB/dist")
http.server.HTTPServer(("127.0.0.1", $PORT), H).serve_forever()
PYEOF
python3 "$SB/server.py" "$SB/priv.pem" & SRV_PID=$!
sleep 1.5
chk "manifest server answers" "curl -s -X POST http://127.0.0.1:$PORT/ -d '{}' | grep -q signature"

echo "=== 2. a box, migrated to the release layout ==="
BOX="$SB/box"
mkdir -p "$BOX/opt/adiona" "$BOX/etc/adiona" "$BOX/etc/systemd/system" "$BOX/var" "$BOX/run" "$SB/bin"
for d in web controller kiosk wheel first-boot updater; do mkdir -p "$BOX/opt/adiona/$d"; done
cp -r web/.               "$BOX/opt/adiona/web/"
cp -r controller/.        "$BOX/opt/adiona/controller/"
cp -r system/kiosk/.      "$BOX/opt/adiona/kiosk/"
cp -r system/wheel/.      "$BOX/opt/adiona/wheel/"
cp -r system/first-boot/. "$BOX/opt/adiona/first-boot/"
cp -r system/updater/.    "$BOX/opt/adiona/updater/"
cp VERSION "$BOX/opt/adiona/VERSION"
cp config/box.conf "$BOX/etc/adiona/box.conf"
cp "$SB/pub.pem" "$BOX/etc/adiona/update-key.pub"
echo "1.5.0" > "$BOX/etc/adiona/image-version"
echo "Adiona-TV-A3F1" > "$BOX/etc/adiona/ssid"
for u in controller kiosk wheel updater; do
	printf '[Unit]\nDescription=adiona-%s v1.5.0\n' "$u" > "$BOX/etc/systemd/system/adiona-$u.service"
done
sed -i "s#^UPDATE_MANIFEST_URL=.*#UPDATE_MANIFEST_URL=\"http://127.0.0.1:$PORT/\"#" \
	"$BOX/etc/adiona/box.conf"

# Stubs for the things a sandbox has no business really doing.
cat > "$SB/bin/systemctl" <<'EOF'
#!/bin/sh
case "$1" in is-active) echo active ;; show) echo 0 ;;
             restart) echo "restart $2" >> "$RESTARTS" ;; esac
exit 0
EOF
cat > "$SB/bin/curl" <<'EOF'
#!/bin/sh
printf '{"mode":"waiting","target":null,"casting":false,"ui_seen_ago":0.4,"uplink":{"internet":true}}'
EOF
printf '#!/bin/sh\nexit 0\n' > "$SB/bin/dpkg"
chmod +x "$SB"/bin/*

export ADIONA_ROOT="$BOX/opt/adiona" ADIONA_ETC="$BOX/etc/adiona" \
       ADIONA_STATE_DIR="$BOX/var" ADIONA_RUN_DIR="$BOX/run" \
       ADIONA_SYSTEMD_DIR="$BOX/etc/systemd/system" ADIONA_CONF="$BOX/etc/adiona/box.conf" \
       ADIONA_SSID_FILE="$BOX/etc/adiona/ssid" \
       ADIONA_UPDATE_KEY="$BOX/etc/adiona/update-key.pub" \
       RESTARTS="$SB/restarts.log"
: > "$RESTARTS"
bash "$BOX/opt/adiona/updater/apply-update.sh" --migrate >/dev/null 2>&1
chk "migrated to the release layout" '[ "$(readlink "$BOX/opt/adiona/current")" = "releases/1.5.0" ]'

echo "=== 3. the updater: check, verify, download, stage ==="
PATH="$SB/bin:$PATH" python3 - <<'PY'
import importlib.util, os, time
spec = importlib.util.spec_from_file_location("au", os.environ["ADIONA_ROOT"] + "/updater/adiona-updater.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
# The controller is not running here; hand over what a live one would report.
m.controller_state = lambda: {"target": None, "casting": False, "uplink": {"internet": True}}
up = m.Updater()
up.check(force=True)
print("  after check :", m.STATE["state"], "->", m.STATE["available"])
up.accept("1.6.0")                       # this starts the download thread itself
for _ in range(600):
    if m.STATE["state"] in ("staged", "failed"):
        break
    time.sleep(0.1)
print("  after stage :", m.STATE["state"], "|", m.STATE["message"])
PY
chk "release 1.6.0 staged"        '[ -d "$BOX/opt/adiona/releases/1.6.0" ]'
chk "staged VERSION is 1.6.0"     '[ "$(cat "$BOX/opt/adiona/releases/1.6.0/VERSION")" = "1.6.0" ]'
chk "no .partial left behind"     '[ ! -d "$BOX/opt/adiona/releases/1.6.0.partial" ]'
chk "tarball cleaned up"          '[ -z "$(ls "$BOX/var/downloads" 2>/dev/null)" ]'
chk "units extracted"             '[ -f "$BOX/opt/adiona/releases/1.6.0/units/adiona-updater.service" ]'
chk "nothing swapped yet"         '[ "$(cat "$BOX/opt/adiona/VERSION")" = "1.5.0" ]'

echo "=== 4. apply ==="
PATH="$SB/bin:$PATH" ADIONA_SOAK_SECONDS=4 ADIONA_SOAK_POLL=1 ADIONA_CONTROLLER_WAIT=3 \
	bash "$BOX/opt/adiona/updater/apply-update.sh" --protocol 1 --from 1.5.0 --to 1.6.0 \
	     --release-dir "$BOX/opt/adiona/releases/1.6.0" > "$SB/apply.log" 2>&1
grep -E "kiosk subtree|updated " "$SB/apply.log" | sed 's/^/     /'
chk "current -> 1.6.0"             '[ "$(readlink "$BOX/opt/adiona/current")" = "releases/1.6.0" ]'
chk "VERSION now 1.6.0"            '[ "$(cat "$BOX/opt/adiona/VERSION")" = "1.6.0" ]'
chk "new page content is live"     'grep -q "Waiting for a headset (v1.6.0)" "$BOX/opt/adiona/web/index.html"'
chk "kiosk unchanged, not restarted" '! grep -q "restart adiona-kiosk" "$RESTARTS"'
chk "controller restarted"         'grep -q "restart adiona-controller" "$RESTARTS"'
chk "marker cleared"               '[ ! -f "$BOX/etc/adiona/update-pending" ]'
chk "result recorded ok"           'grep -q "\"ok\":true" "$BOX/var/last-apply.json"'
chk ".updated stamp written"       'grep -q "v1.6.0 applied over v1.5.0" "$BOX/etc/adiona/.updated"'
chk "operator box.conf preserved"  "grep -q 'UPDATE_MANIFEST_URL=\"http://127.0.0.1:$PORT/\"' \"\$BOX/etc/adiona/box.conf\""

echo "=== 5. a manifest signed with the WRONG key must be refused ==="
kill "$SRV_PID" 2>/dev/null; sleep 0.5
python3 "$SB/server.py" "$SB/evil.pem" & SRV_PID=$!
sleep 1.5
PATH="$SB/bin:$PATH" python3 - <<'PY'
import importlib.util, os
spec = importlib.util.spec_from_file_location("au", os.environ["ADIONA_ROOT"] + "/updater/adiona-updater.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.controller_state = lambda: {"target": None, "casting": False, "uplink": {"internet": True}}
m.Updater().check(force=True)
open(os.environ["ADIONA_STATE_DIR"] + "/tamper.txt", "w").write(
    m.STATE["state"] + "|" + m.STATE["blocked_reason"])
PY
chk "foreign key -> blocked"  'grep -q "^blocked|signature does not verify" "$BOX/var/tamper.txt"'
chk "still on 1.6.0, nothing installed" '[ "$(cat "$BOX/opt/adiona/VERSION")" = "1.6.0" ]'

echo "=== 6. manual rollback ==="
: > "$RESTARTS"
PATH="$SB/bin:$PATH" bash "$BOX/opt/adiona/updater/apply-update.sh" --rollback \
	> "$SB/rollback.log" 2>&1
chk "current -> 1.5.0"        '[ "$(readlink "$BOX/opt/adiona/current")" = "releases/1.5.0" ]'
chk "VERSION back to 1.5.0"   '[ "$(cat "$BOX/opt/adiona/VERSION")" = "1.5.0" ]'
chk "old page content back"   '! grep -q "Waiting for a headset (v1.6.0)" "$BOX/opt/adiona/web/index.html"'

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
