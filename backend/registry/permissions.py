"""Explicit registry capabilities; no role-name bypass at the API boundary."""
from .errors import RegistryError


REGISTRY_PERMISSIONS = ("registry.read", "registry.wells.manage", "registry.devices.manage",
                        "registry.bindings.manage", "registry.maps.manage", "registry.review")
REGISTRY_ROLE_PERMISSIONS = {
    "administrator": REGISTRY_PERMISSIONS,
    "production_engineer": ("registry.read", "registry.wells.manage", "registry.bindings.manage", "registry.review"),
    "algorithm_engineer": ("registry.read", "registry.maps.manage"),
    "maintenance_engineer": ("registry.read", "registry.devices.manage", "registry.bindings.manage", "registry.maps.manage"),
    "reviewer": ("registry.read", "registry.review"),
}
_EXTRA = {
    "well.create": "registry.wells.manage", "well.change": "registry.wells.manage",
    "device.create": "registry.devices.manage", "device.change": "registry.devices.manage",
    "device.bind": "registry.bindings.manage", "device.unbind": "registry.bindings.manage",
    "map.create": "registry.maps.manage", "map.assign": "registry.maps.manage", "map.review": "registry.review",
    "resolved_telemetry": "telemetry.read", "link_status": "acquisition.read", "audit": "audit.read",
}
_READ = {"wells", "devices", "device", "state_history", "bindings", "point_maps", "map_assignments"}


def required_permissions(action: str, body: dict) -> tuple[tuple[str, ...], ...]:
    base = (("registry.read",),)
    if action in _READ:
        return base
    if action in _EXTRA:
        return base + ((_EXTRA[action],),)
    if action == "device.state":
        if body.get("status") == "suspended":
            return base + (("registry.devices.manage", "registry.review"),)
        if body.get("status") in ("active", "retired"):
            return base + (("registry.review",),)
        raise RegistryError("registry.invalid_state", "不支持的台账状态")
    raise RegistryError("registry.not_found", "不支持的台账操作", 404)
