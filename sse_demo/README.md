# SSE demo

## Build image

```bash
docker build -f docker/sse/Dockerfile -t sse-demo:latest .
```

If you use kind or k3d, load the image into the cluster:

```bash
kind load docker-image sse-demo:latest
```

## Create a kind cluster (3 nodes)

```bash
kind create cluster --config kind/kind-config.yaml
```

## Deploy to Kubernetes

```bash
kubectl apply -f k8s/sse-demo.yaml
kubectl apply -f k8s/nginx-proxy.yaml
kubectl rollout status deploy/sse-demo
kubectl rollout status deploy/sse-nginx
```

## Port forward (nginx proxy)

```bash
kubectl port-forward svc/sse-nginx 18080:80
```

## Scale-down test with multiple threads

```bash
python scripts/sse_scale_test.py --url http://localhost:18080/sse --duration 180 --threads 30 --scale-after 10 --scale-to 1
```

If all requests finish without errors, you should see `ok: 30/30`.
