"""Safe, stable errors shared by the control plane and HTTP layer."""


class ControlPlaneError(Exception):
    status_code = 503
    code = "unavailable"


class WireGuardError(ControlPlaneError):
    code = "wireguard_unavailable"


class StorageError(ControlPlaneError):
    code = "storage_unavailable"


class ConflictError(ControlPlaneError):
    status_code = 409
    code = "conflict"


class NotFoundError(ControlPlaneError):
    status_code = 404
    code = "not_found"


class InputError(ControlPlaneError):
    status_code = 422
    code = "invalid_input"
