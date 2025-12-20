#!/usr/bin/env python3
import argparse
import http.client
import threading
import time
import urllib.parse
import subprocess


def read_sse(url: str, timeout: int, results: list, idx: int, verbose: bool) -> None:
    parsed = urllib.parse.urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname, port, timeout=timeout)
    start = time.time()
    done = False

    try:
        conn.putrequest("GET", path)
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("Cache-Control", "no-cache")
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            results[idx] = f"bad status {resp.status}"
            return

        while time.time() - start < timeout:
            line = resp.readline()
            if not line:
                break
            if verbose:
                print(f"[{idx}] {line.strip().decode('utf-8', errors='replace')}")
            if line.strip() == b"event: done":
                done = True
            if done and line.strip() == b"":
                results[idx] = "ok"
                return
        results[idx] = "timeout"
    except Exception as exc:
        results[idx] = f"error {exc}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:18080/sse?duration=180")
    parser.add_argument("--threads", type=int, default=20)
    parser.add_argument("--scale-after", type=int, default=10)
    parser.add_argument("--scale-to", type=int, default=1)
    parser.add_argument("--deployment", default="sse-demo")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--timeout", type=int, default=210)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    results = ["pending"] * args.threads
    threads = []

    for i in range(args.threads):
        t = threading.Thread(
            target=read_sse,
            args=(args.url, args.timeout, results, i, args.verbose),
            daemon=True,
        )
        threads.append(t)
        t.start()

    time.sleep(args.scale_after)
    subprocess.run(
        [
            "kubectl",
            "scale",
            f"deployment/{args.deployment}",
            f"--replicas={args.scale_to}",
            "-n",
            args.namespace,
        ],
        check=False,
    )

    for t in threads:
        t.join()

    ok = sum(1 for r in results if r == "ok")
    failures = [r for r in results if r != "ok"]
    print(f"ok: {ok}/{args.threads}")
    if failures:
        print("failures:")
        for r in failures:
            print(f"- {r}")


if __name__ == "__main__":
    main()
