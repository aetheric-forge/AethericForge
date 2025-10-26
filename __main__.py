import pulumi

from util.naming import with_suffix

def require(cfg: pulumi.Config, key: str) -> str:
    val = cfg.get(key)
    if val is None:
        raise RuntimeError(f"Missing required config: {cfg.name}:{key}")
    return val

cfg = pulumi.Config("aetheric")

cluster_name = require(cfg, "clusterName")
cluster_region = require(cfg, "clusterRegion")
doks_name = with_suffix(cluster_name, "prod")

from workloads.compute import ensure_cluster, attach_node_pools
from workloads.networking import ensure_vpc

vpc = ensure_vpc(name=cluster_name, region=cluster_region)
cluster, k8s_provider = ensure_cluster(
    vpc_id=vpc.id,
)
attach_node_pools(
    cluster=cluster,
)
