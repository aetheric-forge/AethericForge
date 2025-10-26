import pulumi
from pulumi_kubernetes import Provider as K8sProvider, apps, core, networking
from pulumi_kubernetes.apps.v1 import StatefulSetSpecArgs
from pulumi_kubernetes.core.v1 import ContainerArgs, ContainerPortArgs, EnvFromSourceArgs, HTTPGetActionArgs, PersistentVolumeClaimArgs, PersistentVolumeClaimSpecArgs, PodSecurityContextArgs, PodSpecArgs, PodTemplateSpecArgs, ProbeArgs, ResourceRequirementsArgs, SecretEnvSourceArgs, SecurityContextArgs, ServicePortArgs, ServiceSpecArgs, TolerationArgs, VolumeMountArgs, VolumeResourceRequirementsArgs
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
    replicas    = int(require(cfg, "minioReplicas") or 1)  # stick to 1 unless you configure distributed mode
    size_gi     = str(require(cfg, "minioSizeGi") or "50") # PVC size
    sc_name     = require(cfg, "minioStorageClass") or "do-block-storage-retain"
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

    ns = core.v1.Namespace(
        "storage",
        metadata=ObjectMetaArgs(name="storage", labels={"aetheric": aetheric_label}),
        opts=pulumi.ResourceOptions(provider=k8s),
    )

    root_secret = core.v1.Secret(
        "minio-root",
        metadata=ObjectMetaArgs(namespace=ns.metadata["name"]),
        string_data={
            "MINIO_ROOT_USER": admin_user,
            "MINIO_ROOT_PASSWORD": secret_val,
        },
        opts=pulumi.ResourceOptions(provider=k8s),
    )

    svc = core.v1.Service(
        "minio-svc",
        metadata=ObjectMetaArgs(namespace=ns.metadata["name"], labels={"aetheric": aetheric_label}),
        spec=ServiceSpecArgs(
            type="ClusterIP",
            ports=[
                ServicePortArgs(port=9000, target_port=9000, name="s3"),
                ServicePortArgs(port=9001, target_port=9001, name="console")
            ],
            selector={"app": "minio"},
        ),
        opts=pulumi.ResourceOptions(provider=k8s),
    )

    # Headless service for StatefulSet DNS (even single-replica, it’s harmless)
    headless = core.v1.Service(
        "minio-hs",
        metadata=ObjectMetaArgs(namespace=ns.metadata["name"], labels={"app": "minio"}),
        spec=ServiceSpecArgs(
            cluster_ip="None",
            ports=[ServicePortArgs(port=9000, target_port=9000, name="s3")],
            selector={"app": "minio"},
        ),
        opts=pulumi.ResourceOptions(provider=k8s),
    )

    # StatefulSet
    ss = apps.v1.StatefulSet(
        "minio",
        metadata=ObjectMetaArgs(
            namespace=ns.metadata["name"],
            labels={"app": "minio", "aetheric": aetheric_label, "tier": "storage"},
        ),
        spec=StatefulSetSpecArgs(
            service_name=headless.metadata["name"],
            replicas=replicas,
            selector=LabelSelectorArgs(match_labels={"app": "minio"}),
            template=PodTemplateSpecArgs(
                metadata=ObjectMetaArgs(labels={"app": "minio"}),
                spec=PodSpecArgs(
                    node_selector={"doks.digitalocean.com/node-pool": "aetheric-storage"},
                    tolerations=[TolerationArgs(key="pool", operator="Equal", value="storage", effect="NoSchedule")],
                    security_context=PodSecurityContextArgs(fs_group=1000),
                    containers=[
                        ContainerArgs(
                            name="minio",
                            image="minio/minio:RELEASE.2025-09-07T16-13-09Z-cpuv1",
                            args=["server", "/data", "--console-address", ":9001"],
                            env_from=[
                                EnvFromSourceArgs(secret_ref=SecretEnvSourceArgs(name=root_secret.metadata["name"]))
                            ],
                            ports=[
                                ContainerPortArgs(container_port=9000, name="s3")
                            ],
                            liveness_probe=ProbeArgs(
                                http_get=HTTPGetActionArgs(path="/minio/health/live", port="s3"),
                                initial_delay_seconds=20,
                                period_seconds=10,
                            ),
                            startup_probe=ProbeArgs(
                                http_get=HTTPGetActionArgs(path="/minio/health/ready", port="s3"),
                                initial_delay_seconds=20,
                                period_seconds=10,
                            ),
                            volume_mounts=[
                                VolumeMountArgs(
                                    name="data",
                                    mount_path="/data",
                                )
                            ],
                            resources=ResourceRequirementsArgs(
                                requests={"cpu": "200m", "memory": "512Mi"},
                                limits={"cpu": "2", "memory": "4Gi"},
                            )
                        )
                    ]
                ),
            ),
            volume_claim_templates=[PersistentVolumeClaimArgs(
                metadata=ObjectMetaArgs(name="data"),
                spec=PersistentVolumeClaimSpecArgs(
                    access_modes=["ReadWriteOnce"],
                    storage_class_name=sc_name,
                    resources=VolumeResourceRequirementsArgs(
                        requests={"storage": f"{size_gi}Gi"}
                    ),
                )
            )]
        ),
        opts=pulumi.ResourceOptions(provider=k8s),
    )

    if s3_host:
        networking.v1.Ingress(
            "minio-s3-ing",
            metadata=ObjectMetaArgs(
                namespace=ns.metadata["name"],
                annotations={
                    "kubernetes.io/ingress.class": "nginx",
                    "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                    # ExternalDNS picks up host automatically
                },
            ),
            spec=IngressSpecArgs(
                tls=[IngressTLSArgs(hosts=[s3_host], secret_name="minio-tls")],
                rules=[IngressRuleArgs(
                    host=s3_host,
                    http=HTTPIngressRuleValueArgs(
                        paths=[
                            HTTPIngressPathArgs(
                                path="/",
                                path_type="Prefix",
                                backend=IngressBackendArgs(
                                    service=IngressServiceBackendArgs(
                                        name=svc.metadata["name"],
                                        port=ServiceBackendPortArgs(number=svc.spec.apply(lambda s: int(s["ports"][0].port))),
                                    )
                                )
                            ),
                        ],
                    )
                )],
            ),
            opts=pulumi.ResourceOptions(provider=k8s, depends_on=[svc, ss]),
        )

    if console_host:
        networking.v1.Ingress(
            "minio-console-ing",
            metadata=ObjectMetaArgs(
                namespace=ns.metadata["name"],
                annotations={
                    "kubernetes.io/ingress.class": "nginx",
                    "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                    # ExternalDNS picks up host automatically
                },
            ),
            spec=IngressSpecArgs(
                tls=[IngressTLSArgs(hosts=[console_host], secret_name="minio-tls")],
                rules=[IngressRuleArgs(
                    host=console_host,
                    http=HTTPIngressRuleValueArgs(
                        paths=[
                            HTTPIngressPathArgs(
                                path="/",
                                path_type="Prefix",
                                backend=IngressBackendArgs(
                                    service=IngressServiceBackendArgs(
                                        name=svc.metadata["name"],
                                        port=ServiceBackendPortArgs(number=svc.spec.apply(lambda s: int(s["ports"][1].port)))
                                    )
                                )
                            ),
                        ],
                    )
                )],
            ),
            opts=pulumi.ResourceOptions(provider=k8s, depends_on=[svc, ss]),
        )

    pulumi.export("minioEndpoint", pulumi.Output.concat("http://", svc.metadata["name"], ".storage.svc:9000"))
    if s3_host:
        pulumi.export("minioS3", f"https://{s3_host}")
    if console_host:
        pulumi.export("minioConsole", f"https://{console_host}")
