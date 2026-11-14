#!/bin/sh
# Publish from an H800 checkout (or an uploaded source archive).
set -eu
cd "$(dirname "$0")"
root=/workspace/gzqherry/h800-atlas-team
umask 022
install -d -m 755 "$root" "$root/releases"
exec 9>"$root/publish.lock"
flock -x 9
reader="$root/runtime/wandb-0.23.1"
if ! "$reader/bin/python" -I -c 'from wandb.sdk.internal.datastore import DataStore; from wandb.proto import wandb_internal_pb2' 2>/dev/null; then
    python3 -m venv "$reader"
    "$reader/bin/python" -m pip install 'wandb==0.23.1'
fi
files="training_eta.py swan_history.py wandb_history.py wandb_runs.py charts.py gateway.py server.py monitor.py insights.py cpu_usage.py nas.py scheduler.py dist/index.html dist/login.html dist/charts.js dist/charts.css dist/avatars.js dist/crew.webp dist/lunar-cover.webp dist/login-landscape.webp dist/guide.js dist/guide.css dist/guide.html dist/guide-images/*.webp"
version=$(sha256sum $files | sha256sum | cut -c1-16)
release="$root/releases/$version"
stage=$(mktemp -d "$root/releases/.stage-XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/dist/guide-images"
for file in $files; do install -m 644 "$file" "$stage/$file"; done
python3 - "$stage" <<'PY'
import base64, pathlib, sys
root=pathlib.Path(sys.argv[1])
for path in root.glob('*.py'):compile(path.read_bytes(),str(path),'exec')
page=root/'dist/index.html'
html=page.read_text()
for marker,asset in [('<!-- ATLAS_GUIDE_HTML -->','guide.html'),('/* ATLAS_GUIDE_CSS */','guide.css'),('/* ATLAS_GUIDE_JS */','guide.js'),('/* ATLAS_CHARTS_JS */','charts.js'),('/* ATLAS_AVATARS_JS */','avatars.js'),('/* ATLAS_CHARTS_CSS */','charts.css')]:
    assert html.count(marker)==1
    html=html.replace(marker,(page.parent/asset).read_text())
assert html.count('__ATLAS_CREW__')==1
html=html.replace('__ATLAS_CREW__',base64.b64encode((page.parent/'crew.webp').read_bytes()).decode('ascii'))
login=page.parent/'login.html'
for target,template,marker,asset in [(page,html,'__ATLAS_LUNAR_COVER__','lunar-cover.webp'),
                                     (login,login.read_text(),'__ATLAS_LOGIN_LANDSCAPE__','login-landscape.webp')]:
    image=base64.b64encode((page.parent/asset).read_bytes()).decode('ascii')
    assert template.count(marker)==1
    target.write_text(template.replace(marker,image))
assert '__ATLAS_RELEASE__' in html
PY
chmod 755 "$stage"
if [ ! -d "$release" ]; then mv -T "$stage" "$release"; fi
awk '{print "127.0.0.1 " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub >"$root/known_hosts.new"
mv -f "$root/known_hosts.new" "$root/known_hosts"
old=$(readlink -f "$root/current" || true)
if [ -f "$old/dist/avatars.js" ] && grep -Fq '(hash>>>0)%16' "$old/dist/avatars.js"; then
    # Keep existing H800 accounts on the same subject when enlarging the default pool.
    python3 - <<'PY'
import json, os, pathlib, pwd, re, subprocess, tempfile
target = pathlib.Path.home()/'.local/share/h800-atlas/avatars'
target.mkdir(parents=True, mode=0o700, exist_ok=True)
users = {p.pw_name for p in pwd.getpwall() if 1000 <= p.pw_uid < 65534}
users.update(subprocess.check_output(['sacct','-a','-X','-nP','-S','2000-01-01','-o','User'], text=True).splitlines())
for user in users:
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}', user):
        continue
    path = target/(user+'.json')
    if path.exists():
        continue
    value = 2166136261
    for ch in 'atlas-lunar-crew-v1/'+user:
        value = ((value ^ ord(ch))*16777619) & 0xffffffff
    with tempfile.NamedTemporaryFile(mode='w', dir=target) as tmp:
        json.dump(value % 16, tmp)
        tmp.flush()
        try:
            os.link(tmp.name, path)
        except FileExistsError:
            pass
PY
fi
ln -sfn "releases/$version" "$root/current.new"
mv -Tf "$root/current.new" "$root/current"
unitdir="$HOME/.config/systemd/user"
mkdir -p "$unitdir"
install -m 644 h800-atlas.service "$unitdir/h800-atlas.service"
install -d -m 2750 "$root/samples"
install -m 644 h800-atlas-sample.service h800-atlas-sample.timer "$unitdir/"
systemctl --user daemon-reload
systemctl --user enable h800-atlas.service
if [ -f "$old/gateway.py" ] && cmp -s "$old/gateway.py" "$release/gateway.py"; then
    systemctl --user start h800-atlas.service
else
    systemctl --user restart h800-atlas.service
fi
for attempt in 1 2 3 4 5; do
    if curl --noproxy '*' -fsS http://127.0.0.1:18768/ | grep -q 'id="login"'; then
        systemctl --user enable --now h800-atlas-sample.timer
        systemctl --user start h800-atlas-sample.service
        echo "H800 Atlas release $version"
        exit 0
    fi
    sleep 1
done
if [ -d "$old" ] && [ -f "$old/gateway.py" ] && [ "$old" != "$release" ]; then
    ln -sfn "$old" "$root/current.previous"
    mv -Tf "$root/current.previous" "$root/current"
    systemctl --user restart h800-atlas.service
fi
echo 'Gateway health check failed.' >&2
exit 1
