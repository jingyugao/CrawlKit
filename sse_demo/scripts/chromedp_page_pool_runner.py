import argparse
import ast
import csv
import statistics
import subprocess
import time


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}"
        )
    return result


def restart_pods(namespace: str, label: str, timeout: int) -> None:
    run_cmd(["kubectl", "-n", namespace, "delete", "pod", "-l", label], check=False)
    run_cmd(
        [
            "kubectl",
            "-n",
            namespace,
            "wait",
            "--for=condition=Ready",
            "pod",
            "-l",
            label,
            f"--timeout={timeout}s",
        ]
    )


def wait_metrics_ready(namespace: str, label: str, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_cmd(
            ["kubectl", "-n", namespace, "top", "pod", "-l", label, "--no-headers"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return
        time.sleep(1)
    raise RuntimeError("metrics not ready for pod")


def parse_result(output: str) -> dict:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("[result] scenario"):
            parts = line.split(" ", 3)
            if len(parts) < 4:
                continue
            payload = parts[3]
            try:
                return ast.literal_eval(payload)
            except Exception:
                return {}
    return {}


def run_scenario(args: argparse.Namespace, scenario: str) -> dict:
    cmd = [
        args.python,
        "sse_demo/scripts/chromedp_page_pool_demo.py",
        "--namespace",
        args.namespace,
        "--service",
        args.service,
        "--label",
        args.label,
        "--cdp",
        args.cdp,
        "--tasks",
        str(args.tasks),
        "--pages",
        str(args.pages),
        "--concurrency",
        str(args.concurrency),
        "--nav-timeout-ms",
        str(args.nav_timeout_ms),
        "--scenario",
        scenario,
    ]
    cmd.extend(["--url", args.url])
    if args.wait_for_url:
        cmd.extend(["--wait-for-url", args.wait_for_url])
    print(f"[runner] start scenario {scenario}")
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    return parse_result(result.stdout)


def run_warmup(args: argparse.Namespace) -> None:
    if args.warmup_tasks <= 0:
        return
    cmd = [
        args.python,
        "sse_demo/scripts/chromedp_page_pool_demo.py",
        "--namespace",
        args.namespace,
        "--service",
        args.service,
        "--label",
        args.label,
        "--cdp",
        args.cdp,
        "--tasks",
        "0",
        "--pages",
        str(args.pages),
        "--concurrency",
        str(args.concurrency),
        "--nav-timeout-ms",
        str(args.nav_timeout_ms),
        "--scenario",
        "A",
        "--url",
        args.url,
        "--warmup-tasks",
        str(args.warmup_tasks),
    ]
    if args.wait_for_url:
        cmd.extend(["--wait-for-url", args.wait_for_url])
    print("[runner] warmup start")
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run page pool scenarios in separate processes")
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--service", default="mychrome")
    parser.add_argument("--label", default="app=mychrome")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--tasks", type=int, default=400)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--nav-timeout-ms", type=int, default=8000)
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument("--pod-restart-timeout", type=int, default=120)
    parser.add_argument("--metrics-timeout", type=int, default=30)
    parser.add_argument("--csv", default="sse_demo/scripts/chromedp_page_pool_results.csv")
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--wait-for-url", default="")
    parser.add_argument("--warmup-tasks", type=int, default=0)
    args = parser.parse_args()

    order = ["B", "A"]
    rows: list[dict] = []
    for idx in range(args.iterations):
        scenario = order[idx % 2]
        restart_pods(args.namespace, args.label, args.pod_restart_timeout)
        wait_metrics_ready(args.namespace, args.label, args.metrics_timeout)
        run_warmup(args)
        summary = run_scenario(args, scenario)
        row = {
            "iteration": idx + 1,
            "scenario": scenario,
            "samples": summary.get("samples", 0),
            "cpu_avg_millicores": summary.get("cpu_avg_millicores", 0),
            "cpu_max_millicores": summary.get("cpu_max_millicores", 0),
            "mem_avg_bytes": summary.get("mem_avg_bytes", 0),
            "mem_max_bytes": summary.get("mem_max_bytes", 0),
            "nav_avg_ms": summary.get("nav_avg_ms", 0),
            "nav_p50_ms": summary.get("nav_p50_ms", 0),
            "nav_p95_ms": summary.get("nav_p95_ms", 0),
            "nav_max_ms": summary.get("nav_max_ms", 0),
        }
        rows.append(row)
        time.sleep(2)

    with open(args.csv, "w", newline="") as csvfile:
        fieldnames = [
            "iteration",
            "scenario",
            "samples",
            "cpu_avg_millicores",
            "cpu_max_millicores",
            "mem_avg_bytes",
            "mem_max_bytes",
            "nav_avg_ms",
            "nav_p50_ms",
            "nav_p95_ms",
            "nav_max_ms",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    def summarize(metric: str, scenario: str) -> tuple[float, float]:
        values = [row[metric] for row in rows if row["scenario"] == scenario]
        if not values:
            return 0.0, 0.0
        return statistics.mean(values), statistics.pstdev(values)

    for scenario in ("A", "B"):
        cpu_avg_mean, cpu_avg_std = summarize("cpu_avg_millicores", scenario)
        mem_avg_mean, mem_avg_std = summarize("mem_avg_bytes", scenario)
        nav_avg_mean, nav_avg_std = summarize("nav_avg_ms", scenario)
        nav_p95_mean, nav_p95_std = summarize("nav_p95_ms", scenario)
        print(
            f"[summary] scenario {scenario} cpu_avg mean={cpu_avg_mean:.2f}m std={cpu_avg_std:.2f}m "
            f"mem_avg mean={mem_avg_mean:.2f}B std={mem_avg_std:.2f}B "
            f"nav_avg mean={nav_avg_mean:.2f}ms std={nav_avg_std:.2f}ms "
            f"nav_p95 mean={nav_p95_mean:.2f}ms std={nav_p95_std:.2f}ms"
        )

    paired = []
    for i in range(1, args.iterations + 1):
        a_rows = [r for r in rows if r["iteration"] == i and r["scenario"] == "A"]
        b_rows = [r for r in rows if r["iteration"] == i and r["scenario"] == "B"]
        if a_rows and b_rows:
            paired.append((a_rows[0], b_rows[0]))
    if paired:
        diffs_avg = [p[0]["nav_avg_ms"] - p[1]["nav_avg_ms"] for p in paired]
        diffs_p95 = [p[0]["nav_p95_ms"] - p[1]["nav_p95_ms"] for p in paired]
        print(
            f"[paired] nav_avg A-B mean={statistics.mean(diffs_avg):.2f}ms std={statistics.pstdev(diffs_avg):.2f}ms"
        )
        print(
            f"[paired] nav_p95 A-B mean={statistics.mean(diffs_p95):.2f}ms std={statistics.pstdev(diffs_p95):.2f}ms"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
