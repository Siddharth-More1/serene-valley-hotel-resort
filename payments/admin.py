from django.contrib import admin

from accounts.models import User
from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    # Provider identifiers and secrets are intentionally omitted from the UI.
    list_display = ("id", "kind", "booking", "invoice", "amount", "status", "created_at")
    list_filter = ("kind", "status", ("created_at", admin.DateFieldListFilter))
    search_fields = (
        "booking__id",
        "booking__customer__username",
        "booking__customer__email",
        "booking__room__room_number",
    )
    fields = ("kind", "booking", "invoice", "amount", "status", "created_at")
    readonly_fields = fields
    date_hierarchy = "created_at"
    list_select_related = ("booking", "booking__customer", "booking__room", "invoice")

    def _can_view_payments(self, request):
        return request.user.is_superuser or (
            request.user.is_staff and request.user.role == User.Role.MANAGER
        )

    def has_module_permission(self, request):
        return self._can_view_payments(request)

    def has_view_permission(self, request, obj=None):
        return self._can_view_payments(request)

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
