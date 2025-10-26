import pulumi
import pulumi_digitalocean as do

from util.naming import with_suffix

def ensure_vpc(*, name: str, region: str) -> do.Vpc:
    vpc_name = with_suffix(f"{name}-{region}", "vpc")
    return do.Vpc(
        vpc_name,
        name=vpc_name,
        region=region,
        ip_range="10.77.0.0/16",
        description="Aetheric Forge VPC",
    )
