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
