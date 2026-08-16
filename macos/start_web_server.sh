#!/bin/zsh
set -euo pipefail

# Run the browser backend under launchd so closing a terminal cannot stop it.
ROOT="${1:?project root is required}"
PORT="${2:-8765}"
PYTHON="$ROOT/.venv/bin/python3"
LABEL="com.sensus.electrochem-workstation.web"
UID_VALUE="$(id -u)"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/SensUs Workstation"

if [[ ! -x "$PYTHON" ]]; then
  print -u2 "Python environment not found: $PYTHON"
  exit 1
fi
mkdir -p "$AGENT_DIR" "$LOG_DIR"

"$PYTHON" -c 'import plistlib,sys; from pathlib import Path; p,r,port,log,label=sys.argv[1:]; root=Path(r); payload={"Label":label,"ProgramArguments":[str(root/".venv/bin/python3"),"-m","pa_host.gui_server","--host","127.0.0.1","--port",port,"--transport","auto"],"WorkingDirectory":r,"EnvironmentVariables":{"SENSUS_PROJECT_DIR":r,"PYTHONPATH":str(root/"software/host"),"PYTHONUNBUFFERED":"1"},"RunAtLoad":True,"KeepAlive":{"SuccessfulExit":False},"ThrottleInterval":5,"ProcessType":"Interactive","StandardOutPath":str(Path(log)/("web-"+port+".log")),"StandardErrorPath":str(Path(log)/("web-"+port+".log"))}; Path(p).write_bytes(plistlib.dumps(payload,fmt=plistlib.FMT_XML,sort_keys=False))' "$PLIST" "$ROOT" "$PORT" "$LOG_DIR" "$LABEL"

launchctl bootout "gui/$UID_VALUE/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_VALUE" "$PLIST"
launchctl kickstart -k "gui/$UID_VALUE/$LABEL"

for _ in {1..40}; do
  if curl -fsS --max-time 0.4 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT/"
    print "SensUs 工作站已常驻运行: http://127.0.0.1:$PORT/"
    exit 0
  fi
  sleep 0.15
done
print -u2 "工作站启动超时，日志: $LOG_DIR/web-$PORT.log"
exit 1
