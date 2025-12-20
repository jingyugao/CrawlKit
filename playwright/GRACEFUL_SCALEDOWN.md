# Graceful Scale-Down for Playwright Pods (K8S)

This document summarizes the mechanisms used in this repo to keep a Playwright
client stable while scaling down Chrome pods, and the principles behind it.

## Goal

When scaling down Playwright pods (e.g. `chromedp-headless`), the Python client
must not crash and should avoid failed page navigations during the scale-down
window.

## Key Ideas

1. **Client-side draining**
   - When an endpoint disappears from discovery, mark it as "draining".
   - A draining endpoint:
     - Does not receive new page allocations.
     - Keeps existing pages alive until they finish.
     - Is cleaned up only after active pages drop to zero.

2. **CDP disconnect detection**
   - Listen to `browser.on("disconnected")` from Playwright.
   - Mark endpoint unhealthy immediately and purge idle resources.
   - This prevents new allocations to a dead browser.

3. **Stable endpoint discovery**
   - Discover endpoints from K8S via `kubectl` (pods or service endpoints).
   - Cache the last known good list and reuse it if discovery returns empty.
   - This prevents transient empty lists from forcing a global cleanup.

4. **Acquire fallback across endpoints**
   - If a target endpoint closes mid-acquire, mark it unhealthy and retry on
     another endpoint inside the pool (not as an application-level retry).
   - This keeps the outer request flow clean while shielding from transient
     endpoint failure.

## K8S Lifecycle Controls

1. **preStop without early kill**
   - Do not `kill -TERM` inside preStop.
   - Use a sleep to allow active pages to finish:
     - Example: `sleep 45`

2. **Termination grace period**
   - Must exceed worst-case page duration.
   - Example: 60-120 seconds or more if needed.

3. **Avoid forced deletion**
   - Do not use `kubectl delete pod --force`.
   - Forced deletion bypasses the grace period and will cut live sessions.

## Implementation in This Repo

1. **Endpoint draining in the pool**
   - `pagepool/pool.py` tracks `_draining_endpoints`.
   - Removed endpoints are first marked draining if they still have active
     pages, then cleaned after active pages reach zero.
   - Draining endpoints are excluded from load balancing and page spawning.

2. **CDP disconnect hook**
   - `pagepool/wrappers.py` registers `browser.on("disconnected")`.
   - The pool logs and cleans up that endpoint immediately.

3. **Discovery fallback**
   - `playwright/k8s_playwright_demo.py` keeps `last_good` endpoints.
   - If discovery returns empty, it reuses `last_good` instead of clearing.

4. **Scale-down logs**
   - Logs show:
     - `endpoint draining` when a pod is removed
     - `endpoint disconnected` when CDP closes
     - `endpoint cleanup` after resources are reclaimed

## Recommended Flow

1. Scale down normally:
   - `kubectl scale deploy/chromedp-headless --replicas=<n>`

2. Observe logs:
   - `kubectl logs deploy/playwright-demo -f`
   - Expect to see draining/disconnect/cleanup in that order.

3. If you still see errors:
   - Increase `preStop` sleep.
   - Increase `terminationGracePeriodSeconds`.
   - Ensure endpoint discovery is stable (service endpoints are preferred).

## Notes

- Even with a perfect client-side drain, a sudden endpoint kill will still
  cause failures. The K8S lifecycle must allow time to finish in-flight work.
- For production, consider using a Service and watching Endpoints instead of
  raw Pod IPs for more stable discovery.
