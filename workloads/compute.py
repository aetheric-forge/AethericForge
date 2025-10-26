# compute.py
from __future__ import annotations
import re
import pulumi
import pulumi_kubernetes as k8s
import pulumi_digitalocean as do
from typing import Any, Dict, List, Optional

from util.naming import with_suffix

# ---- Config helpers ---------------------------------------------------------

AETHERIC = pulumi.Config("aetheric")

def _require(cfg: pulumi.Config, key: str) -> str:
    v = cfg.get(key)
    if v is None:
        raise RuntimeError(f"Missing required config: {cfg.name}:{key}")
    return v

def load_base_config() -> Dict[str, Any]:
    return {
        "name": _require(AETHERIC, "clusterName"),
        "region": _require(AETHERIC, "clusterRegion"),
        "nodePools": AETHERIC.get_object("nodePools") or [],
    }

def split_pools(pools: List[Dict[str, Any]]):
    system = [p for p in pools if p.get("isSystem")]
    if len(system) != 1:
        raise ValueError(f"Expected exactly one system pool, found {len(system)}")
    system_pool = system[0]
    other_pools = [p for p in pools if not p.get("isSystem")]
    return system_pool, other_pools

# ---- Node pool args builders ------------------------------------------------

def _labels_for(name: str, pool_name: str, extra: Optional[Any] = None) -> Dict[str, str]:
    base: Dict[str, str] = {"aetheric": str(name), "aetheric-pool": str(pool_name)}
    if not extra:
        return base

    def put(k: Any, v: Any = True):
        # DO expects strings
        base[str(k)] = "true" if v is True else str(v)

    if isinstance(extra, dict):
        for k, v in extra.items():
            put(k, v)

    elif isinstance(extra, list):
        # allow: [{"env":"prod"}, "gpu=true", "trace"]  -> trace => "true"
        for item in extra:
            if isinstance(item, dict):
                for k, v in item.items():
                    put(k, v)
            elif isinstance(item, str):
                if "=" in item:
                    k, v = item.split("=", 1)
                    put(k.strip(), v.strip())
                else:
                    put(item.strip())

    elif isinstance(extra, str):
        # allow: "env=prod,team=core trace"
        for token in re.split(r"[,\s]+", extra.strip()):
            if not token:
                continue
            if "=" in token:
                k, v = token.split("=", 1)
                put(k.strip(), v.strip())
            else:
                put(token.strip())

    else:
        raise TypeError(f"Unsupported labels type: {type(extra)}")

    return base

def _taints_from(p: Dict[str, Any], default_if_system: bool) -> Optional[List[do.KubernetesNodePoolTaintArgs]]:
    # Allow config to specify taints; otherwise, if this is the system pool and
    # no taints were provided, apply a protective NoSchedule taint by default.
    taints_cfg = p.get("taints")
    if isinstance(taints_cfg, list) and taints_cfg:
        return [
            do.KubernetesNodePoolTaintArgs(
                key=t.get("key"),
                value=t.get("value"),
                effect=t.get("effect", "NoSchedule"),
            )
            for t in taints_cfg
        ]
    if default_if_system:
        return [do.KubernetesNodePoolTaintArgs(key="pool", value="system", effect="NoSchedule")]
    return None

def _np_common_kwargs(name: str, pool: Dict[str, Any], *, is_system: bool):
    pool_name = pool["name"]
    size      = pool["size"]
    min_nodes = int(pool.get("minNodes", 1))
    max_nodes = int(pool.get("maxNodes", min_nodes))
    autoscale = bool(pool.get("autoScale", min_nodes != max_nodes))  # infer if not set
    labels    = _labels_for(name, pool_name, pool.get("labels"))

    kwargs: Dict[str, Any] = dict(
        name=f"{name}-{pool_name}",
        size=size,
        labels=labels,
        tags=["aetheric", name, "pool", pool_name],
        auto_scale=autoscale,
    )
    if autoscale:
        kwargs.update(dict(min_nodes=min_nodes, max_nodes=max_nodes))
    else:
        kwargs.update(dict(node_count=min_nodes))

    taints = _taints_from(pool, default_if_system=is_system)
    if taints:
        kwargs["taints"] = taints
    return kwargs

# ---- Main entry points ------------------------------------------------------

def ensure_cluster(*, vpc_id: pulumi.Input[str]) -> tuple[do.KubernetesCluster, k8s.Provider]:
    cfg = load_base_config()
    name, region = cfg["name"], cfg["region"]
    system_pool_cfg, _ = split_pools(cfg["nodePools"])

    # Pick a valid DOKS version slug dynamically (avoid guessing).
    versions = do.get_kubernetes_versions()
    version = versions.latest_version  # or filter for a series if you prefer

    pulumi.log.info(f"Using DOKS version: {version}")

    default_np = do.KubernetesClusterNodePoolArgs(
        **_np_common_kwargs(name, system_pool_cfg, is_system=True)
    )

    doks_name = with_suffix(name, "doks")
    cluster = do.KubernetesCluster(
        doks_name,
        name=name,
        region=region,
        vpc_uuid=vpc_id,
        version=version,
        node_pool=default_np,  # default/system pool at creation
        tags=["aetheric", name, "doks"],
    )

    kubeconfig = pulumi.Output.secret(cluster.kube_configs[0].raw_config)
    provider = k8s.Provider(f"{name}-k8s", kubeconfig=kubeconfig, enable_server_side_apply=True)
    pulumi.export("kubeconfig", kubeconfig)
    return cluster, provider

def attach_node_pools(*, cluster: do.KubernetesCluster):
    cfg = load_base_config()
    name = cfg["name"]
    _, other_pools = split_pools(cfg["nodePools"])

    for p in other_pools:
        do.KubernetesNodePool(
            f"{name}-{p['name']}",
            cluster_id=cluster.id,
            **_np_common_kwargs(name, p, is_system=False),
        )
