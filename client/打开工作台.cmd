@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
set "H800_ACCOUNT="
set /p "H800_ACCOUNT=H800 SSH username: "
powershell.exe -NoProfile -Command "if ($env:H800_ACCOUNT -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$') { exit 1 }"
if errorlevel 1 goto failed
echo After SSH connects, open http://127.0.0.1:18769/ and keep this window open.
ssh.exe -N -T -o UserKnownHostsFile=known_hosts -o GlobalKnownHostsFile=NUL -o HostKeyAlgorithms=ssh-ed25519 -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -p 60011 -L 127.0.0.1:18769:127.0.0.1:18768 -l "%H800_ACCOUNT%" 36.103.247.214
if not errorlevel 1 exit /b
:failed
echo Connection failed. Check your username, network and local port 18769.
pause
