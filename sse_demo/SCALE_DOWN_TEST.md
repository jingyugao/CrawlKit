# SSE scale-down test principles

This document focuses on the techniques and principles used to validate SSE behavior during Kubernetes scale-down, without step-by-step operations.

## SSE stream integrity

SSE is a long-lived HTTP response where events are framed by blank lines. A robust integrity check needs a deterministic signal for both correctness and completion:

- **Monotonic sequence**: each `tick` event carries a strictly increasing `seq`. If the client sees a decrease, a duplicate, or a gap, it indicates a broken stream, buffering issue, or reconnection.
- **Terminal event**: a final `done` event tells the client the stream ended normally. Ending without `done` indicates truncation.
- **No mid-stream host changes**: a stable upstream Pod for the duration of the stream implies the proxy did not splice or reconnect the stream mid-way.

## Proving which Pod served the stream

Kubernetes exposes the Pod name as `HOSTNAME` by default. Emitting this value inside each SSE event payload provides a cryptographically simple but operationally strong signal:

- The client can **attribute each stream** to a specific Pod.
- If a Pod is later scaled down, you can still **prove that streams were served by that Pod** because the hostname was embedded in the stream itself.
- Seeing multiple hostnames across parallel streams **proves load distribution** across replicas.

## Graceful scale-down behavior

Scale-down should not break in-flight streams. The key mechanics are:

- **Readiness gating**: when shutdown begins, the app returns 503 on `/ready`, which removes the Pod from the Service endpoints. This prevents *new* connections from landing on a terminating Pod.
- **Termination grace period**: Kubernetes sends `SIGTERM` and waits before forcible termination. The app uses this window to let active streams finish.
- **Active connection tracking**: the app tracks open streams and waits for them to drain (up to a configured timeout) before exit, minimizing abrupt disconnects.

## Proxy behavior for SSE

Default proxy buffering can coalesce or delay SSE frames, which makes it look like events are missing or delayed. The proxy must be SSE-friendly:

- **Disable buffering** (`proxy_buffering off`, `proxy_request_buffering off`) so events flush immediately.
- **Keep-alive and timeouts** are extended to handle long-lived connections without idle disconnects.
- **HTTP/1.1 streaming** ensures chunked transfer works correctly for continuous event delivery.

## Validation logic at the client

To distinguish normal completion from truncation or proxy errors, the client validates:

- strict `seq` ordering (no gaps or backtracking),
- presence of the terminal `done`,
- stable `hostname` for a given stream,
- and any HTTP status or network error.

Together, these principles provide end-to-end evidence that scale-down does not introduce partial messages or failed streams, and that traffic did reach the scaled-down Pods.
