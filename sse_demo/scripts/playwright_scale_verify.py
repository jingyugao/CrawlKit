import argparse
import asyncio
import json
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib import request

from playwright.async_api import async_playwright


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}"
        )
    return result


def wait_pod_ready(label: str, namespace: str, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_cmd(
            ["kubectl", "-n", namespace, "get", "pods", "-l", label, "-o", "json"],
            check=False,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            items = payload.get("items", [])
            if items:
                status = items[0].get("status", {})
                conditions = status.get("conditions", [])
                for condition in conditions:
                    if condition.get("type") == "Ready" and condition.get("status") == "True":
                        return
        time.sleep(2)
    raise TimeoutError("pod not ready in time")


def get_first_pod_name(label: str, namespace: str) -> Optional[str]:
    result = run_cmd(
        ["kubectl", "-n", namespace, "get", "pods", "-l", label, "-o", "json"],
        check=False,
    )
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    items = payload.get("items", [])
    if not items:
        return None
    return items[0]["metadata"]["name"]


def poll_pod_termination(name: str, namespace: str, timeout: int) -> Optional[dict]:
    deadline = time.time() + timeout
    last_terminated = None
    while time.time() < deadline:
        result = run_cmd(
            ["kubectl", "-n", namespace, "get", "pod", name, "-o", "json"], check=False
        )
        if result.returncode != 0:
            time.sleep(0.5)
            continue
        payload = json.loads(result.stdout)
        statuses = payload.get("status", {}).get("containerStatuses", [])
        if statuses:
            state = statuses[0].get("state", {})
            if "terminated" in state:
                return state["terminated"]
            last = statuses[0].get("lastState", {})
            if "terminated" in last:
                last_terminated = last["terminated"]
        time.sleep(0.5)
    return last_terminated


def start_port_forward(namespace: str, service: str, local_port: int, remote_port: int):
    cmd = [
        "kubectl",
        "-n",
        namespace,
        "port-forward",
        f"svc/{service}",
        f"{local_port}:{remote_port}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect(("127.0.0.1", local_port))
                return proc
            except OSError:
                time.sleep(0.3)
                continue
    proc.terminate()
    raise RuntimeError("port-forward not ready")


async def connect_and_wait_disconnect(
    cdp_endpoint: str, timeout: int, connected_event: Optional[asyncio.Event]
) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        disconnected = asyncio.Event()

        def on_disconnect() -> None:
            print(f"[{utc_now()}] received playwright disconnected")
            disconnected.set()

        browser.on("disconnected", on_disconnect)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        await page.goto("about:blank")
        print(f"[{utc_now()}] connected to {cdp_endpoint}")
        if connected_event is not None:
            connected_event.set()

        try:
            await asyncio.wait_for(disconnected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


def wait_cdp_ready(cdp_endpoint: str, timeout: int) -> None:
    deadline = time.time() + timeout
    url = f"{cdp_endpoint.rstrip('/')}/json/version"
    while time.time() < deadline:
        try:
            with request.urlopen(url, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except Exception:
            time.sleep(1)
    raise TimeoutError("cdp endpoint not ready in time")


async def run_verification(
    namespace: str,
    deployment: str,
    service: str,
    label: str,
    cdp_endpoint: str,
    timeout: int,
) -> int:
    wait_pod_ready(label, namespace, timeout)
    pod_name = get_first_pod_name(label, namespace)
    if not pod_name:
        raise RuntimeError("no pod found")

    port_forward = start_port_forward(namespace, service, 9222, 9222)
    try:
        await asyncio.to_thread(wait_cdp_ready, cdp_endpoint, timeout)
        connected_event = asyncio.Event()
        disconnect_task = asyncio.create_task(
            connect_and_wait_disconnect(cdp_endpoint, timeout, connected_event)
        )

        try:
            await asyncio.wait_for(connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"[{utc_now()}] playwright failed to connect in time")
            return 1

        await asyncio.to_thread(
            run_cmd, ["kubectl", "-n", namespace, "scale", f"deployment/{deployment}", "--replicas=0"]
        )

        disconnected = await disconnect_task
        if not disconnected:
            print(f"[{utc_now()}] no disconnected signal before timeout")
            return 1

        terminated = await asyncio.to_thread(
            poll_pod_termination, pod_name, namespace, timeout
        )
        if terminated is None:
            print(f"[{utc_now()}] pod termination info not available")
            return 1

        exit_code = terminated.get("exitCode")
        reason = terminated.get("reason")
        signal_code = terminated.get("signal")
        print(
            f"[{utc_now()}] pod exited: exitCode={exit_code}, reason={reason}, signal={signal_code}"
        )

        if exit_code == 0:
            print(f"[{utc_now()}] container exited cleanly after client disconnect")
        elif exit_code == 137 or signal_code == 9:
            print(f"[{utc_now()}] container was SIGKILLed (likely grace timeout)")
        else:
            print(f"[{utc_now()}] container exited with error code")

        return 0
    finally:
        port_forward.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Playwright disconnect on scale down")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--deployment", default="mychrome")
    parser.add_argument("--service", default="mychrome")
    parser.add_argument("--label", default="app=mychrome")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def handle_signal(signum: int, _frame) -> None:
        print(f"[{utc_now()}] received signal {signum}, exiting")
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    async def runner() -> int:
        verify_task = asyncio.create_task(
            run_verification(
                args.namespace,
                args.deployment,
                args.service,
                args.label,
                args.cdp,
                args.timeout,
            )
        )
        stop_task = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {verify_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            return 1
        return verify_task.result()

    return loop.run_until_complete(runner())


if __name__ == "__main__":
    raise SystemExit(main())
