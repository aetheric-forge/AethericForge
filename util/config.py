import pulumi

def require(cfg: pulumi.Config, key: str) -> str:
    val = cfg.get(key)
    if val is None:
        raise RuntimeError(f"Missing required config: {cfg.name}:{key}")
    return val
