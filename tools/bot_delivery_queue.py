"""Durable, opt-in local Bot Chat delivery. POSIX worker; no new model tool."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time

if __name__ == "__main__" and not __package__:
    # Terminal-tool children sanitize PYTHONPATH; the listener must still
    # import the same immutable release as the caller that enqueued it.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

TERMINAL = {"delivered", "failed", "unknown"}
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
JOB_ID = re.compile(r"[a-f0-9-]{36}\Z")


def enabled(home: Path) -> bool:
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    return ((config or {}).get("bot_mode") or {}).get("durable_delivery_queue") is True


def local_target(argv: list[str]) -> str | None:
    if len(argv) < 4 or argv[1] != "-p" or "chat" not in argv:
        return None
    if Path(argv[0]).name not in {"hermes", "hermes.exe"} or not PROFILE.fullmatch(argv[2]):
        return None
    return argv[2]


@contextlib.contextmanager
def _try_lock(path: Path):
    if os.name != "posix":
        raise RuntimeError("durable bot queue requires a POSIX worker")
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Queue:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.directory = self.root / "bot_delivery_queue"
        if self.directory.is_symlink():
            raise ValueError("queue directory must not be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / "queue.db"
        if self.path.is_symlink():
            raise ValueError("queue DB must not be a symlink")
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, target TEXT NOT NULL, source_home TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                owner_pid INTEGER, reason TEXT, notified INTEGER NOT NULL DEFAULT 0
            )""")
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def folder(self, job_id: str) -> Path:
        if not JOB_ID.fullmatch(job_id):
            raise ValueError("invalid delivery ID")
        path = self.directory / job_id
        if path.is_symlink():
            raise ValueError("queue job must not be a symlink")
        return path

    def get(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def list(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created,id")]

    def request(self, job_id: str) -> dict:
        return json.loads((self.folder(job_id) / "request.json").read_text())

    def enqueue(self, argv: list[str], content: str, record: dict, source_home: Path) -> dict:
        from tools import bot_mode_dm as dm
        target = local_target(argv)
        if target is None:
            raise ValueError("only validated local Hermes delivery can be queued")
        source_home = Path(source_home).resolve()
        if source_home != self.root and source_home.parent != self.root / "profiles":
            raise ValueError("source profile outside queue install")
        destination = self.root if target == "default" else self.root / "profiles" / target
        if not destination.is_dir() or destination.is_symlink():
            raise ValueError("destination profile unavailable")
        job_id = record["delivery_id"]
        folder = self.folder(job_id)
        folder.mkdir(mode=0o700, exist_ok=True)
        request = {"argv": argv, "content": content, "record": record}
        # A stable per-delivery lock also serializes enqueue/replay with dispatch.
        with _try_lock(folder / ".lock") as owns:
            if not owns:
                return self.get(job_id)
            try:
                existing = self.get(job_id)
            except KeyError:
                existing = None
            if existing:
                old = self.request(job_id)
                stable = ("delivery_id", "idempotency_key", "origin_session_id", "turn_identity", "accepted_at")
                if (old["argv"] != argv or old["content"] != content
                        or any(old["record"].get(k) != record.get(k) for k in stable)
                        or existing["source_home"] != str(source_home)):
                    raise ValueError("delivery identity rebound")
                return existing
            dm._atomic_json(folder / "request.json", request)
            now = time.time()
            with self.connect() as db:
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,NULL,NULL,0)",
                           (job_id, target, str(source_home), "queued", now, now))
        return self.get(job_id)

    def state(self, job_id: str, state: str, reason: str = ""):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,reason=?,updated=?,owner_pid=? WHERE id=?",
                       (state, reason, time.time(), os.getpid() if state == "running" else None, job_id))

    def head(self, job: dict) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE target=? AND state IN ('queued','running') ORDER BY created,id LIMIT 1",
                             (job["target"],)).fetchone()
        return row is not None and row["id"] == job["id"]

    def target_busy(self, job: dict) -> bool:
        home = self.root if job["target"] == "default" else self.root / "profiles" / job["target"]
        path = home / "state.db"
        if not path.exists():
            return False  # the existing CLI owns canonical-session creation
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
            # Profile-wide is deliberately conservative, matching bot_relay's
            # own lock scope. Never race a human turn or compression commit.
            for table in ("session_turn_leases", "compression_locks"):
                if db.execute(f"SELECT 1 FROM {table} WHERE expires_at>? LIMIT 1", (time.time(),)).fetchone():
                    return True
        return False

    def run_one(self, job_id: str) -> str:
        from tools import bot_mode_dm as dm, bot_relay
        folder = self.folder(job_id)
        with _try_lock(folder / ".lock") as owns:
            if not owns:
                return "busy"
            job = self.get(job_id)
            if job["state"] == "running":
                self.state(job_id, "unknown", "worker_lost_after_dispatch; inspect receipt before any replay")
                return "unknown"
            if job["state"] != "queued" or not self.head(job) or self.target_busy(job):
                return job["state"]
            try:
                with bot_relay.acquire_turn_lock(self.root, job["target"], timeout_seconds=0):
                    if self.target_busy(job):
                        return "queued"
                    # Set the source before importing scope-sensitive transport.
                    # Production runs each job in its own process; no global env
                    # is shared between profiles or worker threads.
                    os.environ["HERMES_HOME"] = job["source_home"]
                    request = self.request(job_id)
                    record = dict(request["record"])
                    record.pop("queue_id", None)
                    dm_file = folder / "query.txt"
                    dm_file.write_text(request["content"], encoding="utf-8")
                    dm_file.chmod(0o600)
                    dm._atomic_json(Path(str(dm_file) + ".receipt.json"), record)
                    if isinstance(record.get("turn_identity"), dict):
                        dm._atomic_json(dm._delivery_envelope_path(str(dm_file)), dm._delivery_envelope_from_record(record))
                    # After this durable transition, a crash is ambiguous. The
                    # queue never invokes its own retry of a dispatched effect.
                    self.state(job_id, "running")
                    try:
                        with (folder / "reply.txt").open("w") as out, (folder / "error.txt").open("w") as err:
                            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                                rc = dm._run_delivery(request["argv"], str(dm_file), stdin_file=False, lock_held=True)
                        self.state(job_id, "delivered" if rc == 0 else "failed", "" if rc == 0 else "transport_failed; original request retained")
                    except BaseException as exc:
                        self.state(job_id, "unknown", "dispatch_interrupted:" + type(exc).__name__)
                        if not isinstance(exc, Exception):
                            raise
                    return self.get(job_id)["state"]
            except bot_relay.TurnBusyError:
                return "queued"

    def notify(self, job: dict):
        from hermes_cli import notification_inbox
        if job["state"] not in TERMINAL or job["notified"]:
            return
        home = Path(job["source_home"])
        request = self.request(job["id"])
        content = (f"Entrega entre agentes: {job['state']}\nDestino: {job['target']}\n"
                   f"ID: {job['id']}\nOrigem: {request['record'].get('origin_session_id', '')}\n"
                   f"{job.get('reason') or 'O retorno está no recibo da entrega.'}")
        notification_inbox.append(home, content, delivery_id="bot-queue:" + job["id"],
                                  source="bot-delivery-queue",
                                  origin_session=request["record"].get("origin_session_id", ""))
        with self.connect() as db:
            db.execute("UPDATE jobs SET notified=1 WHERE id=?", (job["id"],))


def wait_command(root: Path, job_id: str) -> str:
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "wait", job_id])


def wait(queue: Queue, job_id: str) -> int:
    while True:
        job = queue.get(job_id)
        if job["state"] in TERMINAL:
            folder = queue.folder(job_id)
            if job["state"] == "delivered":
                path = folder / "reply.txt"
                print(path.read_text() if path.exists() else json.dumps({"status": "delivered", "delivery_id": job_id}))
                return 0
            print(json.dumps({"status": job["state"], "delivery_id": job_id, "reason": job["reason"],
                              "detail": "Request retained. Do not blindly resend an ambiguous delivery."}))
            return 1
        time.sleep(1)


def serve(queue: Queue):
    from tools import bot_mode_dm as dm
    children: dict[str, subprocess.Popen] = {}
    with _try_lock(queue.directory / ".worker.lock") as owns:
        if not owns:
            raise RuntimeError("queue worker already running")
        while True:
            children = {k: p for k, p in children.items() if p.poll() is None}
            jobs = queue.list()
            for job in jobs:
                if job["state"] in TERMINAL:
                    try:
                        queue.notify(job)
                    except Exception:
                        pass  # durable notified=0 makes the next poll retry
                elif len(children) < 4 and job["id"] not in children:
                    with _try_lock(queue.folder(job["id"]) / ".lock") as idle:
                        if not idle:
                            continue
                    if job["state"] == "queued" and (not queue.head(job) or queue.target_busy(job)):
                        continue
                    env = dict(os.environ, HERMES_HOME=job["source_home"])
                    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
                    log = (queue.folder(job["id"]) / "worker.log").open("a")
                    try:
                        children[job["id"]] = subprocess.Popen(
                            [sys.executable, "-m", "tools.bot_delivery_queue", "--root", str(queue.root), "run", job["id"]],
                            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        )
                    finally:
                        log.close()
            counts = {s: sum(j["state"] == s for j in jobs) for s in {"queued", "running", *TERMINAL}}
            attention = counts["unknown"] + counts["failed"]
            dm._atomic_json(queue.directory / "health.json", {"status": "attention" if attention else "ok", "pid": os.getpid(),
                            "checked_at": time.time(), "counts": counts,
                            "notifications_pending": sum(j["state"] in TERMINAL and not j["notified"] for j in jobs)})
            time.sleep(2)


def main():
    from hermes_constants import get_default_hermes_root
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=get_default_hermes_root())
    parser.add_argument("action", choices=["serve", "run", "wait", "status"])
    parser.add_argument("job_id", nargs="?")
    args = parser.parse_args()
    queue = Queue(args.root)
    if args.action == "serve":
        serve(queue)
    elif args.action == "run":
        queue.run_one(args.job_id)
    elif args.action == "wait":
        raise SystemExit(wait(queue, args.job_id))
    else:
        print(json.dumps(queue.list()))


if __name__ == "__main__":
    main()
