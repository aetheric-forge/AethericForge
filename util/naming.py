import pulumi

def with_suffix(base: str, suffix: str) -> str:
    # ensure we don’t get "-vpc-vpc" if callers already appended
    return base if base.endswith(suffix) else f"{base}-{suffix}"
