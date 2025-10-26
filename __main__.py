import pulumi
from util.config import require


# load the `aetheric` config namespace
cfg = pulumi.Config("aetheric")

# required config values
cluster_name = require(cfg, "clusterName")
cluster_region = require(cfg, "clusterRegion")

from workloads.system import deploy_cert_manager, deploy_external_dns, deploy_nginx_ingress
from workloads.compute import ensure_cluster, attach_node_pools
from workloads.networking import ensure_vpc

# Stand up the kube cluster
vpc = ensure_vpc(name=cluster_name, region=cluster_region)
cluster, k8s_provider = ensure_cluster(
    vpc_id=vpc.id,
)
attach_node_pools(
    cluster=cluster,
)

# System services for kube
deploy_cert_manager(cfg=cfg, provider=k8s_provider)
deploy_external_dns(cfg=cfg, provider=k8s_provider)
deploy_nginx_ingress(cfg=cfg, provider=k8s_provider)

# MinIO for S3 storage
from workloads.storage import deploy_minio
deploy_minio(cfg=cfg, k8s=k8s_provider)
