#!/usr/bin/env python3
import argparse
import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def build_url(base: str, duration: int, interval: float) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}duration={duration}&interval={interval}"


def parse_event(
    event_name: str,
    data_lines: list[str],
    last_seq: int,
    hostname: str | None,
) -> tuple[bool, int, str | None, str | None]:
    if not data_lines:
        return False, last_seq, hostname, None
    data = "\n".join(data_lines)
    if event_name == "tick":
        try:
            payload = json.loads(data)
            seq = int(payload.get("seq", -1))
            event_host = payload.get("hostname")
        except (ValueError, TypeError, json.JSONDecodeError):
            return False, last_seq, hostname, f"bad tick payload: {data}"
        if seq <= last_seq:
            return False, last_seq, hostname, f"non-monotonic seq: {seq} <= {last_seq}"
        if event_host:
            if hostname and event_host != hostname:
                return False, last_seq, hostname, f"hostname changed: {hostname} -> {event_host}"
            hostname = event_host
        return False, seq, hostname, None
    if event_name == "done":
        try:
            payload = json.loads(data)
            event_host = payload.get("hostname")
        except (ValueError, TypeError, json.JSONDecodeError):
            return True, last_seq, hostname, None
        if event_host:
            if hostname and event_host != hostname:
                return False, last_seq, hostname, f"hostname changed: {hostname} -> {event_host}"
            hostname = event_host
        return True, last_seq, hostname, None
    return False, last_seq, hostname, None


def sse_once(url: str, timeout: float) -> tuple[bool, str | None, str | None]:
    event_name = ""
    data_lines: list[str] = []
    last_seq = -1
    done = False
    hostname: str | None = None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            start = time.time()
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    done, last_seq, hostname, err = parse_event(
                        event_name, data_lines, last_seq, hostname
                    )
                    if err:
                        return False, err, hostname
                    event_name = ""
                    data_lines = []
                    if done:
                        return True, None, hostname
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())
                if time.time() - start > timeout:
                    return False, "timeout", hostname
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}", hostname
    except Exception as exc:  # noqa: BLE001
        return False, f"error: {exc}", hostname

    return False, "stream ended without done", hostname


def worker(
    idx: int,
    url: str,
    timeout: float,
    repeat: int,
    results: list[tuple[bool, str | None, str | None]],
) -> None:
    count = 0
    while repeat == 0 or count < repeat:
        ok, err, hostname = sse_once(url, timeout)
        results.append((ok, err, hostname))
        if not ok:
            return
        count += 1


def scale_after(delay: int, target: str, replicas: int) -> None:
    time.sleep(delay)
    cmd = ["kubectl", "scale", target, f"--replicas={replicas}"]
    subprocess.run(cmd, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="SSE scale-down test")
    parser.add_argument("--url", required=True, help="Base URL, e.g. http://localhost:18080/sse")
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--threads", type=int, default=10)
    parser.add_argument("--repeat", type=int, default=1, help="0 means infinite")
    parser.add_argument("--timeout", type=float, default=220)
    parser.add_argument("--scale-after", type=int, default=0)
    parser.add_argument("--scale-to", type=int, default=0)
    parser.add_argument("--scale-target", default="deployment/sse-demo")
    args = parser.parse_args()

    url = build_url(args.url, args.duration, args.interval)
    results: list[tuple[bool, str | None, str | None]] = []

    if args.scale_after > 0 and args.scale_to > 0:
        t = threading.Thread(
            target=scale_after,
            args=(args.scale_after, args.scale_target, args.scale_to),
            daemon=True,
        )
        t.start()

    threads = []
    for i in range(args.threads):
        t = threading.Thread(target=worker, args=(i, url, args.timeout, args.repeat, results))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    total = len(results)
    ok = sum(1 for item in results if item[0])
    errors = [err for ok_flag, err, _ in results if not ok_flag]
    hostnames: dict[str, int] = {}
    for ok_flag, _, hostname in results:
        if ok_flag and hostname:
            hostnames[hostname] = hostnames.get(hostname, 0) + 1

    print(f"ok: {ok}/{total}")
    if hostnames:
        print("hostnames:")
        for name, count in sorted(hostnames.items()):
            print(f"- {name}: {count}")
    if errors:
        print("errors:")
        for err in errors[:10]:
            print(f"- {err}")
        if len(errors) > 10:
            print(f"- ... and {len(errors) - 10} more")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
