import pulumi
from pulumi_kubernetes import Provider as K8sProvider, apps, core, networking
from pulumi_kubernetes.apps.v1 import StatefulSetSpecArgs
from pulumi_kubernetes.core.v1 import ContainerArgs, ContainerPortArgs, EnvFromSourceArgs, EnvVarArgs, HTTPGetActionArgs, Namespace, PersistentVolumeClaimArgs, PersistentVolumeClaimSpecArgs, PodSecurityContextArgs, PodSpecArgs, PodTemplateSpecArgs, ProbeArgs, ResourceRequirementsArgs, Secret, SecretEnvSourceArgs, SecurityContextArgs, ServicePortArgs, ServiceSpecArgs, TolerationArgs, VolumeMountArgs, VolumeResourceRequirementsArgs
from pulumi_kubernetes.helm.v3 import Chart, ChartOpts, FetchOpts
from pulumi_kubernetes.meta.v1 import LabelSelectorArgs, ObjectMetaArgs
from pulumi_kubernetes.networking.v1 import HTTPIngressPathArgs, HTTPIngressRuleValueArgs, IngressBackendArgs, IngressRuleArgs, IngressServiceBackendArgs, IngressSpecArgs, IngressTLSArgs, ServiceBackendPortArgs
from pulumi_random import RandomPassword

from util.config import require

def deploy_minio(*, cfg: pulumi.Config, k8s: K8sProvider):
    enable_min  = require(cfg, "enableMinio") or False
    if not enable_min:
        pulumi.log.info("enableMinio=false; skipping MinIO")
        return None

    domain      = require(cfg, "domain")
    enable_dns  = require(cfg, "enableExternalDNS") or False
    s3_host        = f"s3.{domain}" if (domain and enable_dns) else None
    console_host = f"console.{s3_host}" if s3_host else None

    # Generate or accept root creds
    admin_user  = require(cfg, "minioRootUser") or "aetheric-minio"
    admin_password  = require(cfg, "minioRootPassword") or RandomPassword("minio-secret", length=24)  # optional: swap to pulumi_random if you’ve added it
    # If you’re not using pulumi-random, just require it from config:
    if isinstance(admin_password, pulumi.CustomResource):
        # user is using pulumi_random; expose value
        secret_val = admin_password.result
    else:
        secret_val = admin_password

    aetheric_label = f"{pulumi.get_project()}-{pulumi.get_stack()}"

    ns = Namespace(
        "storage",
        metadata=ObjectMetaArgs(name="storage", labels={"aetheric": aetheric_label}),
        opts=pulumi.ResourceOptions(provider=k8s),
    )

    Secret(
        "minio-root",
        metadata=ObjectMetaArgs(namespace=ns.metadata["name"], name="minio-root"),
        string_data={
            "rootUser": admin_user,
            "rootPassword": secret_val,
        },
        opts=pulumi.ResourceOptions(provider=k8s),
    )

    lb_annotations = {}
    if s3_host or console_host:
        lb_annotations["external-dns.alpha.kubernetes.io/hostname"] = ",".join(filter(None, [s3_host, console_host]))

    release = Chart(
        "minio",
        ChartOpts(
            chart="minio",
            # version can be pinned later after you pick one you like
            fetch_opts=FetchOpts(repo="https://charts.min.io/"),
            namespace=ns.metadata["name"],
            values={
                # Distributed mode (3 pods, 1 drive each)
                "mode": "distributed",
                "replicas": 3,
                "drivesPerNode": 1,

                # Persistence (PVC per pod)
                "persistence": {
                    "enabled": True,
                    "size": f'{require(cfg, key="minioSizeGi") or 100}Gi',
                    "storageClass": require(cfg, key="minioStorageClass") or "do-block-storage",
                },

                # Credentials via existing Secret (keys must be rootUser/rootPassword)
                "existingSecret": "minio-root",

                # Run on storage pool with your toleration
                "nodeSelector": {"doks.digitalocean.com/node-pool": "aetheric-storage"},
                "tolerations": [{"key": "pool", "operator": "Equal", "value": "storage", "effect": "NoSchedule"}],

                # Expose both API and Console via a DO LoadBalancer (no Ingress)
                "service": {
                    "type": "ClusterIP",
                    "name": "minio-lb",
                    "ports": {"api": 9000, "console": 9001},
                },

                # Headless service / STS knobs the chart handles internally:
                "statefulset": {
                    "podManagementPolicy": "Parallel",
                    # Rolling updates are fine for distributed
                    "updateStrategy": {"type": "RollingUpdate"},
                },
                "resources": {
                    "requests": {
                        "cpu": "200m",
                        "memory": "1Gi",
                    },
                    "limits": {
                        "cpu": "1",
                        "memory": "2Gi",
                    },
                },

                # Tell MinIO its public URLs (stops console/API redirect weirdness)
                "environment": {
                    # set only if you actually have hostnames; otherwise leave out
                    **({"MINIO_SERVER_URL": f"https://{s3_host}"} if s3_host else {}),
                    **({"MINIO_BROWSER_REDIRECT_URL": f"https://{console_host}"} if console_host else {}),
                    **({"MINIO_API_CORS_ALLOW_ORIGIN": f"https://{console_host},https://{s3_host}"} if (s3_host and console_host) else {}),
                },
                # We enable TLS at ingress only
                "tls": { "enabled": False },
                "ingress": {
                    "enabled": True,
                    "ingressClassName": "nginx",
                    "annotations": {
                        "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                        "nginx.ingress.kubernetes.io/ssl-redirect": "true",
                        "nginx.ingress.kubernetes.io/force-ssl-redirect": "true",
                        "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
                        "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
                        "nginx.ingress.kubernetes.io/proxy-request-buffering": "off",
                        "nginx.ingress.kubernetes.io/proxy-body-size": "0",
                        "nginx.ingress.kubernetes.io/proxy-http-version": "1.1",
                        # external-dns will publish both names
                        "external-dns.alpha.kubernetes.io/hostname": s3_host,
                    },
                    "hosts": [s3_host],
                    "tls": [
                        { "secretName": "minio-s3-tls", "hosts": [s3_host] },
                    ],
                },
                "consoleIngress": {
                    "enabled": True,
                    "ingressClassName": "nginx",
                    "annotations": {
                        "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                        "nginx.ingress.kubernetes.io/ssl-redirect": "true",
                        "nginx.ingress.kubernetes.io/force-ssl-redirect": "true",
                        "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
                        "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
                        "nginx.ingress.kubernetes.io/proxy-request-buffering": "off",
                        "nginx.ingress.kubernetes.io/proxy-body-size": "0",
                        "nginx.ingress.kubernetes.io/proxy-http-version": "1.1",
                        # external-dns will publish both names
                        "external-dns.alpha.kubernetes.io/hostname": console_host,
                    },
                    "hosts": [console_host],
                    "tls": [
                        { "secretName": "minio-console-tls", "hosts": [console_host] },
                    ],
                },
                # don't run the post-job as it requires the certs that would have been created if TLS were enabled
                "postJob": {"enabled": False},
            },
        ),
        opts=pulumi.ResourceOptions(provider=k8s, depends_on=[ns]),
    )
    minio_svc = release.get_resource("v1/Service", "storage/minio")
    pulumi.export(
        "minioEndpointInternal",
        pulumi.Output.concat("http://", minio_svc.metadata["name"], ".", minio_svc.metadata["namespace"], ".svc.cluster.local:9000")
    )
    if s3_host:
        pulumi.export("minioS3", f"https://{s3_host}")
    if console_host:
        pulumi.export("minioConsole", f"https://{console_host}")
