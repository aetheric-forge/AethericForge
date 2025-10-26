from platform import node
from util.config import require

import pulumi
import pulumi_kubernetes as k8s
from pulumi_kubernetes.core.v1 import Namespace, Secret
from pulumi_kubernetes.meta.v1 import ObjectMetaArgs
from pulumi_kubernetes.helm.v3 import Chart, ChartOpts, FetchOpts
from pulumi_kubernetes.apiextensions import CustomResource

# Common placement for "system" addons
def _system_scheduling_values():
    return {
        "controller": {
            "tolerations": [{"key": "pool", "operator": "Equal", "value": "system", "effect": "NoSchedule"}],
            "nodeSelector": {"doks.digitalocean.com/node-pool": "aetheric-system"},
        }
    }

def deploy_cert_manager(*, cfg: pulumi.Config, provider: k8s.Provider):
    # Namespace
    ns = Namespace(
        "cert-manager",
        metadata=ObjectMetaArgs(name="cert-manager", labels={"aetheric": f"{pulumi.get_project()}-{pulumi.get_stack()}"}),
        opts=pulumi.ResourceOptions(provider=provider),
    )

    email = require(cfg, key="acmeEmail")

    # Cloudflare API token secret
    cf_token = require(cfg, key="cloudflareApiToken")
    Secret(
        "cf-api-token",
        metadata=ObjectMetaArgs(namespace=ns.metadata["name"], name="cloudflare-api-token"),
        string_data={"api-token": cf_token},
        opts=pulumi.ResourceOptions(provider=provider),
    )

    # Helm chart (Jetstack). Enable CRDs and pin to system pool.
    Chart(
        "cert-manager",
        ChartOpts(
            chart="cert-manager",
            version="v1.15.1",  # pick a current stable
            namespace=ns.metadata["name"],
            fetch_opts=FetchOpts(repo="https://charts.jetstack.io"),
            values={
                "installCRDs": True,
                # scheduler hints for all subcomponents
                "global": {
                    "leaderElection": {"namespace": "cert-manager"},
                },
                **_system_scheduling_values(),
                "webhook": {
                    "tolerations": [{"key": "pool", "operator": "Equal", "value": "system", "effect": "NoSchedule"}],
                    "nodeSelector": {"doks.digitalocean.com/node-pool": "aetheric-system"},
                },
                "cainjector": {
                    "tolerations": [{"key": "pool", "operator": "Equal", "value": "system", "effect": "NoSchedule"}],
                    "nodeSelector": {"doks.digitalocean.com/node-pool": "aetheric-system"},
                },
            },
        ),
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[ns]),
    )

    # ClusterIssuers (HTTP-01 via ingress-nginx)

    for name, server in [
        ("letsencrypt-staging",  "https://acme-staging-v02.api.letsencrypt.org/directory"),
        ("letsencrypt-prod",     "https://acme-v02.api.letsencrypt.org/directory"),
    ]:
        CustomResource(
            name,
            api_version="cert-manager.io/v1",
            kind="ClusterIssuer",
            metadata=ObjectMetaArgs(name=name),
            spec={
                "acme": {
                    "email": email,
                    "server": server,
                    "privateKeySecretRef": {"name": f"{name}-private-key"},
                    "solvers": [{
                        "dns01": {
                            "cloudflare": {
                                "apiTokenSecretRef": {
                                    "name": "cloudflare-api-token", "key": "api-token"
                                }
                            }
                        }
                    }],
                }
            },
            opts=pulumi.ResourceOptions(provider=provider, depends_on=[ns]),
        )

def deploy_nginx_ingress(*, cfg: pulumi.Config, provider: k8s.Provider):
    # Namespace
    ns = Namespace(
        "ingress-nginx",
        metadata=ObjectMetaArgs(name="ingress-nginx", labels={"aetheric": f"{pulumi.get_project()}-{pulumi.get_stack()}"}),
        opts=pulumi.ResourceOptions(provider=provider),
    )

    # Helm chart (ingress-nginx). Pin to system pool.
    Chart(
        "ingress-nginx",
        ChartOpts(
            chart="ingress-nginx",
            version="4.7.0",  # pick a current stable
            namespace=ns.metadata["name"],
            fetch_opts=FetchOpts(repo="https://kubernetes.github.io/ingress-nginx"),
            values=_system_scheduling_values().update({
                "controller": {
                    "service": {
                        "type": "LoadBalancer",
                    },
                    "ingressClass": "nginx",
                    "ingressClassResource": {
                        "name": "nginx",
                        "enabled": True,
                        "default": False,
                    },
                },
            }),
        ),
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[ns]),
    )

def deploy_external_dns(*, cfg: pulumi.Config, provider: k8s.Provider):
    if not (require(cfg, key="enableExternalDNS").lower() in ["1", "true", "yes"]):
        pulumi.log.info("ExternalDNS deployment skipped per configuration.")
        return

    zone = require(cfg, key="domain")
    token = require(cfg, key="cloudflareApiToken")
    owner = f"{pulumi.get_project()}-{pulumi.get_stack()}"

    # Namespace
    ns = Namespace(
        "external-dns",
        metadata=ObjectMetaArgs(name="external-dns", labels={"aetheric": owner}),
        opts=pulumi.ResourceOptions(provider=provider),
    )

    # Cloudflare token
    Secret(
        "external-dns-credentials",
        metadata=ObjectMetaArgs(namespace=ns.metadata["name"], name="external-dns-credentials"),
        string_data={"CF_API_TOKEN": token},
    )

    # Helm chart (external-dns). Pin to system pool.
    Chart(
        "external-dns",
        ChartOpts(
            chart="external-dns",
            version="1.18.0",  # pick a current stable
            namespace=ns.metadata["name"],
            fetch_opts=FetchOpts(repo="https://kubernetes-sigs.github.io/external-dns/"),
            values={
                **_system_scheduling_values(),
                "provider": "cloudflare",
                "sources": ["ingress", "service"],
                "domainFilters": [require(cfg, key="domain")],
                "txtOwnerId": owner,
                "interval": "1m",
                "env": [{
                    "name": "CF_API_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "external-dns-credentials",
                            "key": "CF_API_TOKEN",
                        }
                    }
                }],
                "tolerations": [{"key": "pool", "operator": "Equal", "value": "system", "effect": "NoSchedule"}],
                "nodeSelector": {"doks.digitalocean.com/node-pool": "aetheric-system"},
            },
        ),
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[ns]),
    )
