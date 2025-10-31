import os


def generate_unique_name(base_name: str, suffix: str) -> str:
    """
    Generate a unique name by appending a suffix and numeric UIN.
    Skips existing paths to avoid collisions.

    Args:
        base_name (str): The input name (e.g. "example")
        suffix (str): The suffix to append before the UIN (e.g. "trans")

    Returns:
        str: A unique name like "example_trans_1"
    """
    candidate = f"{base_name}_{suffix}"
    if not os.path.exists(candidate):
        return candidate

    for i in range(1, 10000):
        candidate = f"{base_name}_{suffix}_{i}"
        if not os.path.exists(candidate):
            return candidate

    raise RuntimeError
