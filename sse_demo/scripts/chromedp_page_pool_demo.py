import argparse
import asyncio
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from playwright.async_api import async_playwright


@dataclass(frozen=True)
class ResourceSample:
    collected_at: float
    cpu_millicores: int
    mem_bytes: int


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}"
        )
    return result


def start_port_forward(namespace: str, target: str, local_port: int, remote_port: int):
    cmd = [
        "kubectl",
        "-n",
        namespace,
        "port-forward",
        target,
        f"{local_port}:{remote_port}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            result = run_cmd(["curl", "-s", f"http://127.0.0.1:{local_port}/json/version"], check=False)
            if result.returncode == 0:
                return proc
        except Exception:
            time.sleep(0.3)
            continue
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError("port-forward not ready")


def get_first_pod_name(namespace: str, label: str) -> Optional[str]:
    result = run_cmd(
        ["kubectl", "-n", namespace, "get", "pods", "-l", label, "-o", "json"],
        check=False,
    )
    if result.returncode != 0:
        return None
    payload = result.stdout
    try:
        import json
        data = json.loads(payload)
    except Exception:
        return None
    items = data.get("items", [])
    if not items:
        return None
    return items[0]["metadata"]["name"]


def parse_cpu(value: str) -> int:
    value = value.strip()
    if value.endswith("m"):
        return int(float(value[:-1]))
    if value.endswith("n"):
        return int(float(value[:-1]) / 1_000_000)
    if value.endswith("u"):
        return int(float(value[:-1]) / 1_000)
    return int(float(value) * 1000)


def parse_mem(value: str) -> int:
    value = value.strip()
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
    }
    for unit, factor in units.items():
        if value.endswith(unit):
            return int(float(value[: -len(unit)]) * factor)
    return int(float(value))


def fetch_resource_sample(namespace: str, label: str) -> Optional[ResourceSample]:
    result = run_cmd(
        ["kubectl", "-n", namespace, "top", "pod", "-l", label, "--no-headers"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    line = result.stdout.strip().splitlines()[0]
    parts = line.split()
    if len(parts) < 3:
        return None
    cpu = parse_cpu(parts[1])
    mem = parse_mem(parts[2])
    return ResourceSample(collected_at=time.time(), cpu_millicores=cpu, mem_bytes=mem)


async def sample_resources(
    namespace: str,
    label: str,
    interval: float,
    stop_event: asyncio.Event,
    samples: list[ResourceSample],
) -> None:
    while not stop_event.is_set():
        sample = await asyncio.to_thread(fetch_resource_sample, namespace, label)
        if sample is not None:
            samples.append(sample)
        await asyncio.sleep(interval)


async def cleanup_page(page) -> None:
    try:
        await page.goto("about:blank")
    except Exception:
        return
    try:
        await page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
    except Exception:
        return


def register_page_hooks(page):
    async def on_request(request):
        _ = request.url

    async def on_response(response):
        _ = response.status

    async def route_handler(route, request):
        await route.continue_()

    page.on("request", on_request)
    page.on("response", on_response)
    return on_request, on_response, route_handler


async def cleanup_page_hooks(page, hooks) -> None:
    on_request, on_response, route_handler = hooks
    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)
    try:
        await page.unroute("**/*", route_handler)
    except Exception:
        return


async def run_scenario(
    name: str,
    cdp_endpoint: str,
    target_url: str,
    wait_for_url: str | None,
    pool_size: int,
    total_tasks: int,
    concurrency: int,
    reuse_pages: bool,
    namespace: str,
    label: str,
    sample_interval: float,
    nav_timeout_ms: int,
    max_uses: int,
    warmup_tasks: int,
    usage_rows: list[dict],
) -> tuple[list[ResourceSample], list[float]]:
    print(f"[{utc_now()}] scenario {name}: starting")
    samples: list[ResourceSample] = []
    timings_ms: list[float] = []
    stop_event = asyncio.Event()
    sampler = asyncio.create_task(
        sample_resources(namespace, label, sample_interval, stop_event, samples)
    )
    fatal_error = asyncio.Event()
    cleanup_tasks: set[asyncio.Task] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        free_pages: asyncio.Queue = asyncio.Queue()
        for _ in range(pool_size):
            page = await context.new_page()
            hooks = register_page_hooks(page)
            await page.route("**/*", hooks[2])
            page._demo_hooks = hooks
            page._demo_uses = 0
            await free_pages.put(page)

        sem = asyncio.Semaphore(concurrency)

        async def track_task(task: asyncio.Task) -> None:
            cleanup_tasks.add(task)

        async def return_after_cleanup(page) -> None:
            await cleanup_page(page)
            await cleanup_page_hooks(page, page._demo_hooks)
            hooks = register_page_hooks(page)
            await page.route("**/*", hooks[2])
            page._demo_hooks = hooks
            await free_pages.put(page)

        async def close_page(page) -> None:
            if not page.is_closed():
                await page.close()

        async def create_and_return_page() -> None:
            new_page = await context.new_page()
            hooks = register_page_hooks(new_page)
            await new_page.route("**/*", hooks[2])
            new_page._demo_hooks = hooks
            new_page._demo_uses = 0
            await free_pages.put(new_page)

        async def close_and_replace(page) -> None:
            if not page.is_closed():
                await page.close()
            new_page = await context.new_page()
            hooks = register_page_hooks(new_page)
            await new_page.route("**/*", hooks[2])
            new_page._demo_hooks = hooks
            new_page._demo_uses = 0
            await free_pages.put(new_page)

        async def wait_for_frame_url(page, target: str, timeout_ms: int) -> None:
            target_event = asyncio.Event()

            def on_frame(frame) -> None:
                if frame == page.main_frame and frame.url.startswith(target):
                    target_event.set()

            page.on("framenavigated", on_frame)
            try:
                if page.main_frame.url.startswith(target):
                    return
                await asyncio.wait_for(target_event.wait(), timeout=timeout_ms / 1000.0)
            finally:
                page.remove_listener("framenavigated", on_frame)

        async def run_one(idx: int, record: bool) -> None:
            async with sem:
                if fatal_error.is_set():
                    return
                page = await free_pages.get()
                usage_count = getattr(page, "_demo_uses", 0) + 1
                try:
                    started = time.perf_counter()
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=nav_timeout_ms,
                    )
                    if wait_for_url:
                        await wait_for_frame_url(page, wait_for_url, nav_timeout_ms)
                    await page.title()
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    if record:
                        timings_ms.append(elapsed_ms)
                        if usage_rows is not None:
                            usage_rows.append(
                                {"usage": usage_count, "timing_ms": round(elapsed_ms, 2)}
                            )
                except Exception as exc:
                    if exc.__class__.__name__ == "TargetClosedError":
                        print(f"[{utc_now()}] {name}: TargetClosedError")
                        fatal_error.set()
                finally:
                    if reuse_pages:
                        page._demo_uses = usage_count
                        if not page.is_closed():
                            if max_uses > 0 and usage_count >= max_uses:
                                task = asyncio.create_task(close_and_replace(page))
                            else:
                                task = asyncio.create_task(return_after_cleanup(page))
                            await track_task(task)
                        else:
                            if not fatal_error.is_set():
                                task = asyncio.create_task(create_and_return_page())
                                await track_task(task)
                    else:
                        if not fatal_error.is_set():
                            task = asyncio.create_task(close_and_replace(page))
                            await track_task(task)

        if warmup_tasks > 0:
            warmup = [asyncio.create_task(run_one(i, False)) for i in range(warmup_tasks)]
            await asyncio.gather(*warmup, return_exceptions=True)

        tasks = [asyncio.create_task(run_one(i, True)) for i in range(total_tasks)]
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            if fatal_error.is_set():
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                break
        await asyncio.gather(*tasks, return_exceptions=True)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        try:
            await browser.close()
        except Exception:
            pass

    stop_event.set()
    await sampler
    if fatal_error.is_set():
        print(f"[{utc_now()}] scenario {name}: aborted due to browser disconnect")
    print(f"[{utc_now()}] scenario {name}: finished")
    return samples, timings_ms


def summarize_samples(samples: list[ResourceSample]) -> dict:
    if not samples:
        return {}
    cpu_values = [sample.cpu_millicores for sample in samples]
    mem_values = [sample.mem_bytes for sample in samples]
    return {
        "samples": len(samples),
        "cpu_avg_millicores": int(statistics.mean(cpu_values)),
        "cpu_max_millicores": max(cpu_values),
        "mem_avg_bytes": int(statistics.mean(mem_values)),
        "mem_max_bytes": max(mem_values),
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if pct <= 0:
        return ordered[0]
    if pct >= 100:
        return ordered[-1]
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def summarize_timings(timings_ms: list[float]) -> dict:
    if not timings_ms:
        return {}
    return {
        "nav_avg_ms": round(statistics.mean(timings_ms), 2),
        "nav_p50_ms": round(percentile(timings_ms, 50), 2),
        "nav_p95_ms": round(percentile(timings_ms, 95), 2),
        "nav_max_ms": round(max(timings_ms), 2),
    }


async def main_async(args: argparse.Namespace) -> int:
    target = ""
    if args.pod:
        target = f"pod/{args.pod}"
    else:
        pod_name = get_first_pod_name(args.namespace, args.label)
        if pod_name:
            target = f"pod/{pod_name}"
        else:
            target = f"svc/{args.service}"
    port_forward = start_port_forward(args.namespace, target, args.local_port, args.remote_port)
    try:
        scenario_a: tuple[list[ResourceSample], list[float]] = ([], [])
        scenario_b: tuple[list[ResourceSample], list[float]] = ([], [])
        usage_rows: list[dict] = []
        if args.scenario in ("B", "both"):
            scenario_b = await run_scenario(
                name="B(recreate)",
                cdp_endpoint=args.cdp,
                target_url=args.url,
                wait_for_url=args.wait_for_url or None,
                pool_size=args.pages,
                total_tasks=args.tasks,
                concurrency=args.concurrency,
                reuse_pages=False,
                namespace=args.namespace,
                label=args.label,
                sample_interval=args.sample_interval,
                nav_timeout_ms=args.nav_timeout_ms,
                max_uses=0,
                warmup_tasks=args.warmup_tasks,
                usage_rows=[],
            )
            if args.scenario == "both":
                await asyncio.sleep(args.cooldown)
        if args.scenario in ("A", "both"):
            scenario_a = await run_scenario(
                name="A(reuse)",
                cdp_endpoint=args.cdp,
                target_url=args.url,
                wait_for_url=args.wait_for_url or None,
                pool_size=args.pages,
                total_tasks=args.tasks,
                concurrency=args.concurrency,
                reuse_pages=True,
                namespace=args.namespace,
                label=args.label,
                sample_interval=args.sample_interval,
                nav_timeout_ms=args.nav_timeout_ms,
                max_uses=args.max_uses,
                warmup_tasks=args.warmup_tasks,
                usage_rows=usage_rows,
            )
    finally:
        port_forward.terminate()

    if scenario_a[0] or scenario_a[1]:
        summary_a = summarize_samples(scenario_a[0]) | summarize_timings(scenario_a[1])
        print("[result] scenario A", summary_a)
    if scenario_b[0] or scenario_b[1]:
        summary_b = summarize_samples(scenario_b[0]) | summarize_timings(scenario_b[1])
        print("[result] scenario B", summary_b)
    if args.usage_csv and usage_rows:
        import csv
        with open(args.usage_csv, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=["usage", "timing_ms"])
            writer.writeheader()
            writer.writerows(usage_rows)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare page reuse vs recreate on chromedp-headless")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--service", default="chromedp-headless")
    parser.add_argument("--label", default="app=chromedp-headless")
    parser.add_argument("--pod", default="")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--local-port", type=int, default=9222)
    parser.add_argument("--remote-port", type=int, default=9222)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--cooldown", type=float, default=5.0)
    parser.add_argument("--nav-timeout-ms", type=int, default=15000)
    parser.add_argument("--scenario", choices=["A", "B", "both"], default="both")
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--wait-for-url", default="")
    parser.add_argument("--max-uses", type=int, default=0)
    parser.add_argument("--warmup-tasks", type=int, default=0)
    parser.add_argument("--usage-csv", default="")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
