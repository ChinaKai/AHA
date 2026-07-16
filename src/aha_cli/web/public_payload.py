from __future__ import annotations


def redact_hardware_credentials(value: object) -> object:
    """Return a Web-safe copy without exposing hardware login passwords."""
    if isinstance(value, list):
        return [redact_hardware_credentials(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict = {}
    for key, item in value.items():
        if key == "hardware_debug" and isinstance(item, dict):
            hardware = {
                nested_key: redact_hardware_credentials(nested_value)
                for nested_key, nested_value in item.items()
            }
            credentials = hardware.get("credentials")
            if isinstance(credentials, dict):
                password = str(credentials.get("password") or "")
                password_configured = bool(password) or bool(credentials.get("password_configured"))
                credentials["password"] = ""
                credentials["password_configured"] = password_configured
            result[key] = hardware
            continue
        result[key] = redact_hardware_credentials(item)
    return result


__all__ = ["redact_hardware_credentials"]
