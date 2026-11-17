"""Two real processes alternate tracker ownership in a temporary workspace."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def child():
    sys.path.insert(0, str(ROOT / "backend"))
    from soak_workload import isolate_control_paths, verify_control_isolation
    runtime = Path(os.environ["AUTODUB_WORKSPACE"]).parent
    isolate_control_paths(runtime)
    import job_tracker as tracker
    verify_control_isolation(runtime)
    execution = None
    token = None
    for line in sys.stdin:
        command = json.loads(line)
        if command["action"] == "quit":
            break
        if command["action"] == "claim":
            deadline = time.monotonic() + 15
            waiting_reported = False
            while True:
                try:
                    execution = tracker.start_batch(1, job_id=command["id"])
                    token = tracker.current_execution.set(execution)
                    break
                except tracker.JobAlreadyRunningError:
                    if not waiting_reported:
                        print(json.dumps({"event": "blocked"}), flush=True)
                        waiting_reported = True
                    if time.monotonic() > deadline:
                        raise TimeoutError("Peer did not release tracker")
                    time.sleep(.002)
            state = tracker.get_status(strict=True)
            print(json.dumps({"event": "claimed", "id": state["execution_id"], "generation": state["generation"]}), flush=True)
        elif command["action"] == "release":
            tracker.finish_batch()
            tracker.release_batch(execution)
            tracker.current_execution.reset(token)
            print(json.dumps({"event": "released"}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=False)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        return child()
    if args.cycles < 1 or not args.output:
        parser.error("positive --cycles and --output required")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tracker-handoff-") as temporary:
        from soak_dashboard import isolated_environment, MARKER
        runtime = Path(temporary)
        (runtime / MARKER).write_text('generated-only', encoding='utf-8')
        env = isolated_environment(runtime)
        env['PYTEST_CURRENT_TEST'] = 'handoff-stress'
        workspace = Path(env['AUTODUB_WORKSPACE'])
        workers = [subprocess.Popen([sys.executable, "-B", __file__, "--child"], env=env,
                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for _ in range(2)]
        def send(worker, action, identity=None):
            worker.stdin.write(json.dumps({"action": action, "id": identity}) + "\n")
            worker.stdin.flush()
        def receive(worker, expected):
            value = json.loads(worker.stdout.readline())
            if value["event"] != expected:
                raise AssertionError(value)
            return value
        try:
            owner, next_owner = workers
            send(owner, "claim", "initial")
            receive(owner, "claimed")
            for cycle in range(args.cycles):
                identity = "execution-" + str(cycle)
                send(next_owner, "claim", identity)
                receive(next_owner, "blocked")
                send(owner, "release")
                receive(owner, "released")
                claimed = receive(next_owner, "claimed")
                assert claimed["id"] == identity and claimed["generation"] == cycle + 2
                status = json.loads((workspace / "bot_system" / "job_status.json").read_text(encoding="utf-8"))
                lease = json.loads((workspace / "bot_system" / "batch.lock").read_text(encoding="utf-8"))
                assert status["execution_id"] == lease["job_id"] == identity
                assert status["active"] and status["occupies_slot"]
                owner, next_owner = next_owner, owner
            send(owner, "release")
            receive(owner, "released")
            for worker in workers:
                send(worker, "quit")
                _, error = worker.communicate(timeout=15)
                assert worker.returncode == 0, error
            report = {"passed": True, "cycles": args.cycles, "processes": 2,
                      "duration_seconds": round(time.monotonic() - started, 3), "isolated": True,
                      "control_isolation": {"verified_in_both_children": True,
                                            "directory": env['TOOL_V1_CONTROL_DIR']}}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report))
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=10)


if __name__ == "__main__":
    main()
