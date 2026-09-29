def profile_display_name(first_name: str, last_name: str) -> str:
    return f"{first_name.strip()} {last_name.strip()}"


def audit_display_name(first_name: str, last_name: str) -> str:
    return f"{last_name.strip()}, {first_name.strip()}"
