import asyncio
import logging
from typing import AsyncIterator, List

try:
    from kubernetes_asyncio import client, config, watch
    HAS_K8S = True
except ImportError:
    HAS_K8S = False

from pagepool.discovery import EndpointDiscovery
from pagepool.types import EndpointConfig

logger = logging.getLogger(__name__)

class KubernetesEndpointDiscovery(EndpointDiscovery):
    """Watches a Kubernetes Service/Endpoints for pod IPs."""

    def __init__(
        self, 
        service_name: str, 
        namespace: str = "default", 
        port_name: str = "remote-debugging",
        scheme: str = "ws"
    ):
        if not HAS_K8S:
            raise ImportError("kubernetes_asyncio is required for KubernetesEndpointDiscovery")
            
        self.service_name = service_name
        self.namespace = namespace
        self.port_name = port_name
        self.scheme = scheme

    async def watch(self) -> AsyncIterator[List[EndpointConfig]]:
        try:
            # Load in-cluster config or kubeconfig
            try:
                config.load_incluster_config()
            except:
                await config.load_kube_config()
        except Exception as e:
            logger.error(f"Failed to load K8S config: {e}")
            return

        v1 = client.CoreV1Api()
        
        # Initial fetch
        logger.info(f"Starting K8S watch for endpoints: {self.service_name}.{self.namespace}")
        
        # Using a polling loop for simplicity and robustness against watch timeouts
        # A real watch implementation would be better for immediate updates
        while True:
            try:
                endpoints = await self._fetch_endpoints(v1)
                yield endpoints
            except Exception as e:
                logger.error(f"Error fetching k8s endpoints: {e}")
                
            await asyncio.sleep(5)

    async def _fetch_endpoints(self, api) -> List[EndpointConfig]:
        configs = []
        try:
            # Get Endpoints object
            # Note: We watch Endpoints, not Services, to get individual Pod IPs
            eps = await api.read_namespaced_endpoints(self.service_name, self.namespace)
            
            if not eps.subsets:
                return []

            for subset in eps.subsets:
                # Find the target port
                target_port = 9222 # Default
                if subset.ports:
                    for port in subset.ports:
                        if port.name == self.port_name:
                            target_port = port.port
                            break
                
                if not subset.addresses:
                    continue
                    
                for addr in subset.addresses:
                    ip = addr.ip
                    # Construct URL: ws://<pod_ip>:9222
                    url = f"{self.scheme}://{ip}:{target_port}"
                    
                    node_name = addr.node_name
                    
                    cfg = EndpointConfig(
                        url=url,
                        node_name=node_name,
                        tags=["k8s", self.service_name]
                    )
                    configs.append(cfg)
                    
        except Exception as e:
            # If 404, return empty list
            if hasattr(e, "status") and e.status == 404:
                return []
            raise
            
        return configs
