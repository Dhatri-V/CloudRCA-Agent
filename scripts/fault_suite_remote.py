"""Shell scripts the fault suite runs on the Splitwise EC2 instance over SSH (generated here so they can be tested).

Safety properties:
- Every fault runs as a transient systemd unit with RuntimeMaxSec, so systemd ends it at its deadline even if the
  local suite, SSH, or the laptop dies. A SQLite lock held by a killed process is released by the OS.
- The database is copied with SQLite's online backup API, checked with PRAGMA integrity_check, and compared by a
  logical dump hash. Restoration writes back through a SQLite connection (which rolls back any hot journal and
  checkpoints a WAL) instead of copying files, so journal/WAL files are never deleted by hand.
- While the lock scenario is active, an on-instance timer restores the backup by itself if the suite never clears
  its marker. Recovery is verified explicitly and reported; it is never assumed.
"""

from __future__ import annotations

import shlex

APP_DIR = "/home/ec2-user/splitwise-backend"
DB_PATH = f"{APP_DIR}/splitwise.db"
STATE_DIR = "/var/tmp/cloudrca-fault"
BACKUP_PATH = f"{STATE_DIR}/splitwise.db.backup"
BACKUP_HASH = f"{STATE_DIR}/splitwise.db.backup.logical-sha256"
MARKER = f"{STATE_DIR}/db_lock.active"
RESTORE_SCRIPT = f"{STATE_DIR}/restore-db.sh"
UNIT_PREFIX = "cloudrca-fault-"
SAFETY_RESTORE_UNIT = f"{UNIT_PREFIX}db-safety-restore"
FAULT_UNITS = ("traffic", "dblock", "cpu")
SERVICE = "splitwise.service"
API = "http://127.0.0.1:8000"

_LOGICAL_HASH = (
    "import hashlib, sqlite3, sys\n"
    "def logical(path):\n"
    "    connection = sqlite3.connect(path)\n"
    "    try:\n"
    "        ok = connection.execute('PRAGMA integrity_check').fetchone()[0]\n"
    "        digest = hashlib.sha256('\\n'.join(connection.iterdump()).encode()).hexdigest()\n"
    "    finally:\n"
    "        connection.close()\n"
    "    return ok, digest\n"
)


def _python(body: str, user: str = "ec2-user") -> str:
    """Run a Python snippet as ``user`` with the instance's system python3 (no packages are installed)."""
    return f"sudo -u {user} python3 - <<'PY'\n{body}PY\n"


def unit(name: str) -> str:
    return f"{UNIT_PREFIX}{name}"


def status_script() -> str:
    """Read-only: service, fault units, burner processes, marker, guard, and database side files."""
    return (
        "set -u\n"
        f"echo service=$(systemctl is-active {SERVICE} || true)\n"
        f"echo api=$(curl -s -o /dev/null -w '%{{http_code}}' -m 5 {API}/expenses || echo 000)\n"
        f"echo fault_units=$(systemctl list-units --all --no-legend --plain '{UNIT_PREFIX}*' 2>/dev/null | awk '$3==\"active\"{{print $1}}' | paste -sd, -)\n"
        "echo burners=$(pgrep -fc '[c]loudrca-burn' || true)\n"
        f"echo marker=$(test -e {MARKER} && echo present || echo absent)\n"
        f"echo db_side_files=$(ls {DB_PATH}-wal {DB_PATH}-shm {DB_PATH}-journal 2>/dev/null | paste -sd, -)\n"
        "echo shutdown_guard=$(test -e /run/systemd/shutdown/scheduled && echo scheduled || echo none)\n"
        "echo uptime_s=$(cut -d. -f1 /proc/uptime)\n"
    )


def backup_db_script() -> str:
    """Stop the service, copy the DB with the backup API, verify it, and always restart the service."""
    body = _LOGICAL_HASH + (
        f"source, target = {DB_PATH!r}, {BACKUP_PATH!r} + '.tmp'\n"
        "src, dst = sqlite3.connect(source), sqlite3.connect(target)\n"
        "with dst:\n"
        "    src.backup(dst)\n"
        "src.close(); dst.close()\n"
        "ok_src, live = logical(source)\n"
        "ok_dst, copy = logical(target)\n"
        "if ok_src != 'ok' or ok_dst != 'ok' or live != copy:\n"
        "    sys.exit(f'backup verification failed: integrity {ok_src}/{ok_dst}, hashes match={live == copy}')\n"
        "import os\n"
        f"os.replace(target, {BACKUP_PATH!r})\n"
        f"open({BACKUP_HASH!r}, 'w').write(copy)\n"
        "print(f'backup_logical_sha256={copy}')\n"
    )
    return (
        "set -euo pipefail\n"
        f"sudo install -d -o ec2-user -g ec2-user -m 700 {STATE_DIR}\n"
        f"trap 'sudo systemctl start {SERVICE}' EXIT\n"
        f"sudo systemctl stop {SERVICE}\n"
        + _python(body)
        + f"sudo systemctl start {SERVICE}\n"
        "trap - EXIT\n"
        f"for i in $(seq 1 30); do [ \"$(curl -s -o /dev/null -w '%{{http_code}}' -m 2 {API}/expenses)\" = 200 ] && break; sleep 1; done\n"
        f"echo service=$(systemctl is-active {SERVICE})\n"
    )


def restore_db_body() -> str:
    """Restore the backup into the live DB through SQLite, then verify integrity and the logical hash."""
    body = _LOGICAL_HASH + (
        f"expected = open({BACKUP_HASH!r}).read().strip()\n"
        f"live = sqlite3.connect({DB_PATH!r})  # opening rolls back a hot journal left by a killed writer\n"
        "mode = live.execute('PRAGMA journal_mode').fetchone()[0]\n"
        "if mode == 'wal':\n"
        "    live.execute('PRAGMA wal_checkpoint(TRUNCATE)')\n"
        f"backup = sqlite3.connect({BACKUP_PATH!r})\n"
        "with live:\n"
        "    backup.backup(live)\n"
        "backup.close(); live.close()\n"
        f"ok, restored = logical({DB_PATH!r})\n"
        "if ok != 'ok' or restored != expected:\n"
        "    sys.exit(f'restore verification failed: integrity {ok}, hash match={restored == expected}')\n"
        "print(f'restored_logical_sha256={restored}')\n"
    )
    return (
        "set -euo pipefail\n"
        f"for name in {' '.join(unit(item) for item in ('dblock', 'traffic'))}; do sudo systemctl stop \"$name\" 2>/dev/null || true; done\n"
        f"sudo systemctl stop {SERVICE}\n"
        f"trap 'sudo systemctl start {SERVICE}' EXIT\n"
        + _python(body)
        + f"sudo systemctl start {SERVICE}\n"
        "trap - EXIT\n"
        f"sudo rm -f {MARKER}\n"
        f"for i in $(seq 1 30); do [ \"$(curl -s -o /dev/null -w '%{{http_code}}' -m 2 {API}/expenses)\" = 200 ] && break; sleep 1; done\n"
        f"echo service=$(systemctl is-active {SERVICE})\n"
    )


def restore_db_script() -> str:
    """Restore now and disarm the on-instance safety restore."""
    return restore_db_body() + f"sudo systemctl stop {SAFETY_RESTORE_UNIT}.timer {SAFETY_RESTORE_UNIT}.service 2>/dev/null || true\n"


def arm_db_safety_restore(after_seconds: int) -> str:
    """Create the marker and a one-shot timer that restores the backup if the suite never clears the marker."""
    guarded = f"#!/bin/bash\n[ -e {MARKER} ] || exit 0\n" + restore_db_body()
    return (
        "set -euo pipefail\n"
        f"test -s {BACKUP_PATH} && test -s {BACKUP_HASH}\n"
        f"cat > {RESTORE_SCRIPT} <<'RESTORE'\n{guarded}RESTORE\n"
        f"chmod 700 {RESTORE_SCRIPT}\n"
        f"touch {MARKER}\n"
        f"sudo systemd-run --unit={SAFETY_RESTORE_UNIT} --on-active={int(after_seconds)}s --timer-property=AccuracySec=1s /bin/bash {RESTORE_SCRIPT}\n"
        f"echo safety_restore=armed\n"
    )


def _bounded(name: str, seconds: int, command: str, user: str = "ec2-user") -> str:
    """A transient unit that systemd stops at RuntimeMaxSec regardless of the caller."""
    return (
        f"sudo systemd-run --unit={unit(name)} --uid={user} --property=RuntimeMaxSec={int(seconds)} "
        f"--property=KillMode=control-group --collect {command}\n"
    )


def start_traffic(seconds: int, extra: list[tuple[str, str, int]] | None = None, *, post_every: int = 0) -> str:
    """GET /expenses every 15 s; ``extra`` adds (method, path, delay_s) requests; ``post_every`` adds periodic writes."""
    plan = sorted(extra or [], key=lambda item: item[2])
    body = (
        "import json, time, urllib.request\n"
        f"API, end, plan, post_every = {API!r}, time.time() + {int(seconds)}, {plan!r}, {int(post_every)}\n"
        "start = time.time(); next_get = next_post = start\n"
        "def call(method, path, payload=None):\n"
        "    data = json.dumps(payload).encode() if payload is not None else None\n"
        "    request = urllib.request.Request(API + path, data=data, method=method, headers={'Content-Type': 'application/json'})\n"
        "    try:\n"
        "        urllib.request.urlopen(request, timeout=20).read()\n"
        "    except Exception:\n"
        "        pass\n"
        "body = {'title': 'cloudrca-fault-suite', 'amount': 1.0, 'paid_by': 'cloudrca-fault-suite'}\n"
        "while time.time() < end:\n"
        "    now = time.time()\n"
        "    while plan and start + plan[0][2] <= now:\n"
        "        method, path, _ = plan.pop(0); call(method, path, body if method in ('PUT', 'POST') else None)\n"
        "    if now >= next_get:\n"
        "        call('GET', '/expenses'); next_get = now + 15\n"
        "    if post_every and now >= next_post:\n"
        "        call('POST', '/expenses', body); next_post = now + post_every\n"
        "    time.sleep(0.5)\n"
    )
    return _bounded("traffic", seconds + 60, f"python3 -c {shlex.quote(body)}")


def start_lock_holder(seconds: int, hold: int = 8, gap: int = 52) -> str:
    """Repeatedly hold a SQLite write lock longer than the app's busy timeout; released when the process ends."""
    body = (
        "import sqlite3, time\n"
        f"end = time.time() + {int(seconds)}\n"
        "while time.time() < end:\n"
        f"    connection = sqlite3.connect({DB_PATH!r}, timeout=30)\n"
        "    connection.execute('BEGIN IMMEDIATE')\n"
        f"    time.sleep({int(hold)})\n"
        "    connection.rollback(); connection.close()\n"
        f"    time.sleep({int(gap)})\n"
    )
    return _bounded("dblock", seconds + 30, f"python3 -c {shlex.quote(body)}")


def start_cpu_burner(seconds: int, workers: int = 2) -> str:
    """CPU-bound workers named cloudrca-burn; systemd kills the whole control group at RuntimeMaxSec."""
    burn = "exec -a cloudrca-burn python3 -c 'while True: pass'"
    command = "/bin/bash -c " + shlex.quote(" & ".join([f"({burn})"] * int(workers)) + " & wait")
    return _bounded("cpu", seconds, command)


def stop_faults_script() -> str:
    """Stop every fault unit (not the safety restore) and kill stray burners; idempotent."""
    names = " ".join(unit(item) for item in FAULT_UNITS)
    return (
        "set -u\n"
        f"for name in {names}; do sudo systemctl stop \"$name\" 2>/dev/null || true; done\n"
        "sudo pkill -f '[c]loudrca-burn' 2>/dev/null || true\n"
        "echo stopped=1\n"
    )


def arm_shutdown_guard(minutes: int) -> str:
    """Power off (instance stops) after ``minutes`` unless cancelled; only used when the suite started the instance."""
    return f"sudo shutdown -h +{int(minutes)} 'cloudrca fault suite safety guard'\necho shutdown_guard=armed\n"


def cancel_shutdown_guard() -> str:
    return "sudo shutdown -c 2>/dev/null || true\necho shutdown_guard=cancelled\n"


def verify_recovery_script(expected_db_hash: str | None) -> str:
    """Report every recovery property explicitly; the caller treats any failure as manual intervention."""
    body = _LOGICAL_HASH + (
        f"ok, digest = logical({DB_PATH!r})\n"
        "print(f'db_integrity={ok}')\n"
        "print(f'db_logical_sha256={digest}')\n"
    )
    lines = status_script() + _python(body)
    if expected_db_hash:
        lines += f"echo expected_db_logical_sha256={shlex.quote(expected_db_hash)}\n"
    return lines


def parse_status(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(" "))


def recovery_problems(status: dict[str, str]) -> list[str]:
    """Every property that is not back to normal; an empty list is the only success."""
    problems = []
    if status.get("service") != "active":
        problems.append(f"{SERVICE} is {status.get('service', 'unknown')}, expected active")
    if status.get("api") != "200":
        problems.append(f"GET /expenses returned {status.get('api', 'no response')}, expected 200")
    if status.get("fault_units"):
        problems.append(f"fault units still active: {status['fault_units']}")
    if status.get("burners", "0") not in ("", "0"):
        problems.append(f"{status['burners']} CPU burner process(es) still running")
    if status.get("marker") == "present":
        problems.append("database restore marker still present (restore did not complete)")
    if status.get("db_integrity", "ok") != "ok":
        problems.append(f"database integrity_check: {status['db_integrity']}")
    expected = status.get("expected_db_logical_sha256")
    if expected and status.get("db_logical_sha256") != expected:
        problems.append("database content differs from the verified pre-test backup")
    return problems
