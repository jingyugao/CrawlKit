# SSE demo

## Build image

```bash
docker build -f docker/sse/Dockerfile -t sse-demo:latest .
```

If you use kind or k3d, load the image into the cluster:

```bash
kind load docker-image sse-demo:latest
```

## Deploy to Kubernetes

```bash
kubectl apply -f k8s/sse-demo.yaml
kubectl rollout status deploy/sse-demo
```

## Port forward

```bash
kubectl port-forward svc/sse-demo 18080:80
```

## Scale-down test with multiple threads

```bash
python scripts/sse_scale_test.py --url http://localhost:18080/sse?duration=180 --threads 30 --scale-after 10 --scale-to 1
```

If all requests finish without errors, you should see `ok: 30/30`.
