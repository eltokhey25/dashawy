"""Role-based DRF permissions used by the customer and technician APIs."""

from rest_framework.permissions import BasePermission


class IsCustomer(BasePermission):
    message = 'هذه الصفحة متاحة للعملاء فقط'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, 'role', None) == 'customer'
        )


class IsTechnician(BasePermission):
    message = 'هذه الصفحة متاحة للفنيين فقط'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, 'role', None) == 'technician'
        )


class IsAdmin(BasePermission):
    """Admin dashboard access: admin role, or the explicit super-admin flag."""

    message = 'هذه الصفحة متاحة للمسؤولين فقط'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (
                getattr(user, 'role', None) == 'admin'
                or getattr(user, 'is_super_admin', False)
            )
        )


def _admin_permissions(user):
    import json

    try:
        value = json.loads(getattr(user, 'permissions', None) or '[]')
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def AdminPermission(*keys):
    """An admin holding every listed section permission (super admin: all).

    Mirrors ``requirePermission`` from the Node backend, so the per-section
    permissions an admin was given (stats, settings, technicians, ...) are
    enforced by the API and not only hidden in the menu.
    """

    class _AdminPermission(BasePermission):
        message = 'ليس لديك صلاحية للوصول إلى هذا القسم'

        def has_permission(self, request, view):
            user = request.user
            if not (user and user.is_authenticated and getattr(user, 'role', None) == 'admin'):
                return False
            if getattr(user, 'is_super_admin', False):
                return True
            granted = _admin_permissions(user)
            return all(key in granted for key in keys)

    _AdminPermission.__name__ = 'AdminPermission_' + '_'.join(keys)
    return _AdminPermission


class IsSuperAdmin(BasePermission):
    """Only the super admin (admin management)."""

    message = 'هذه الصفحة متاحة فقط للمدير العام'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, 'role', None) == 'admin'
            and getattr(user, 'is_super_admin', False)
        )
