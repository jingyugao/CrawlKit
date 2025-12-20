#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Iterable

from pagepool import PlaywrightPagePool, PoolConfig
from pagepool.load_balancer import random_balancer


@dataclass
class K8sResolverConfig:
    namespace: str
    selector: str | None
    service: str | None
    scheme: str
    port: int
    endpoint_template: str | None
    kube_context: str | None
    kubeconfig: str | None


class K8sEndpointResolver:
    def __init__(self, config: K8sResolverConfig, static_endpoints: list[str] | None):
        self.config = config
        self.static_endpoints = static_endpoints
        self._last_good: list[str] = []

    async def resolve(self) -> list[str]:
        if self.static_endpoints:
            return self.static_endpoints
        endpoints = await asyncio.to_thread(self._resolve_sync)
        if endpoints:
            self._last_good = endpoints
            return endpoints
        return list(self._last_good)

    def _resolve_sync(self) -> list[str]:
        cmd = ["kubectl"]
        if self.config.kube_context:
            cmd += ["--context", self.config.kube_context]
        if self.config.kubeconfig:
            cmd += ["--kubeconfig", self.config.kubeconfig]
        cmd += ["-n", self.config.namespace]

        if self.config.service:
            cmd += ["get", "endpoints", self.config.service, "-o", "json"]
            data = self._run_kubectl(cmd)
            return self._endpoints_from_service(data)

        if not self.config.selector:
            return []
        cmd += ["get", "pods", "-l", self.config.selector, "-o", "json"]
        data = self._run_kubectl(cmd)
        return self._endpoints_from_pods(data)

    def _run_kubectl(self, cmd: list[str]) -> dict:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return {}
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}

    def _endpoint_for_ip(self, ip: str) -> str:
        if self.config.endpoint_template:
            return self.config.endpoint_template.format(ip=ip)
        return f"{self.config.scheme}://{ip}:{self.config.port}"

    def _endpoints_from_service(self, payload: dict) -> list[str]:
        subsets = payload.get("subsets") or []
        endpoints: list[str] = []
        for subset in subsets:
            for addr in subset.get("addresses") or []:
                ip = addr.get("ip")
                if ip:
                    endpoints.append(self._endpoint_for_ip(ip))
        return endpoints

    def _endpoints_from_pods(self, payload: dict) -> list[str]:
        items = payload.get("items") or []
        endpoints: list[str] = []
        for pod in items:
            if pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            if not _pod_ready(pod):
                continue
            ip = pod.get("status", {}).get("podIP")
            if ip:
                endpoints.append(self._endpoint_for_ip(ip))
        return endpoints


def _pod_ready(pod: dict) -> bool:
    for cond in pod.get("status", {}).get("conditions") or []:
        if cond.get("type") == "Ready" and cond.get("status") == "True":
            return True
    return False


async def visit_loop(
    idx: int,
    pool: PlaywrightPagePool,
    url: str,
    pause_seconds: float,
) -> None:
    while True:
        async with pool.page() as page:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status >= 400:
                raise RuntimeError(f"bad response: {getattr(response, 'status', None)}")
            await asyncio.sleep(pause_seconds)


def _parse_endpoints(value: str | None) -> list[str] | None:
    if not value:
        return None
    parts = [item.strip() for item in value.split(",")]
    return [item for item in parts if item]


def _log_endpoints(endpoints: Iterable[str]) -> None:
    joined = ", ".join(endpoints)
    logging.info("active endpoints: %s", joined if joined else "(none)")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Playwright k8s demo with safe scale-down")
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--sleep", type=float, default=5.0)
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--selector", default="app=playwright")
    parser.add_argument("--service", default=None)
    parser.add_argument("--scheme", default="http")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--endpoint-template", default=None)
    parser.add_argument("--endpoints", default=None, help="Comma-separated endpoints to bypass kubectl")
    parser.add_argument("--kube-context", default=None)
    parser.add_argument("--kubeconfig", default=None)
    parser.add_argument("--log-interval", type=float, default=15.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    static_endpoints = _parse_endpoints(args.endpoints)
    resolver_cfg = K8sResolverConfig(
        namespace=args.namespace,
        selector=args.selector,
        service=args.service,
        scheme=args.scheme,
        port=args.port,
        endpoint_template=args.endpoint_template,
        kube_context=args.kube_context,
        kubeconfig=args.kubeconfig,
    )
    resolver = K8sEndpointResolver(resolver_cfg, static_endpoints)

    config = PoolConfig(
        cdp_endpoints=resolver.resolve,
        load_balancer=random_balancer,
        strict_mode=False,
    )

    async with PlaywrightPagePool(config) as pool:
        async with asyncio.TaskGroup() as tg:
            for i in range(args.concurrency):
                tg.create_task(visit_loop(i, pool, args.url, args.sleep))
            tg.create_task(_log_loop(pool, args.log_interval))

    return 0


async def _log_loop(pool: PlaywrightPagePool, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        _log_endpoints(pool.get_stats().keys())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
