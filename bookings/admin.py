from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import User
from payments.models import Payment
from properties.models import Room
from .models import Booking


def validate_status_change(booking, new_status):
    if not booking.can_transition_to(new_status):
        raise ValidationError(
            f"A booking cannot move from {booking.get_status_display()} to "
            f"{dict(Booking.Status.choices).get(new_status, new_status)}."
        )
    if new_status == booking.status:
        return

    if new_status in {Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN}:
        conflicts = Booking.objects.filter(
            room_id=booking.room_id,
            check_in__lt=booking.check_out,
            check_out__gt=booking.check_in,
        ).exclude(pk=booking.pk).exclude(
            status__in=[Booking.Status.CANCELLED, Booking.Status.CHECKED_OUT]
        )
        if conflicts.exists():
            raise ValidationError(
                "This room has another active booking for overlapping dates. "
                "Resolve the conflict before changing this booking's status."
            )

    if new_status == Booking.Status.CANCELLED:
        unsettled_or_captured = booking.payments.filter(
            status__in=[Payment.Status.PENDING, Payment.Status.SUCCESS]
        ).exists()
        if unsettled_or_captured:
            raise ValidationError(
                "This booking has a pending or successful payment. Resolve the "
                "payment or complete a refund before cancelling the booking."
            )

    if new_status == Booking.Status.CHECKED_IN and not booking.payments.filter(
        kind=Payment.Kind.ROOM, status=Payment.Status.SUCCESS
    ).exists():
        raise ValidationError(
            "A verified successful payment is required before check-in."
        )

    if new_status == Booking.Status.CHECKED_OUT:
        from operations.models import CheckoutInvoice
        invoice = getattr(booking, "checkout_invoice", None)
        if invoice is None or invoice.status != CheckoutInvoice.Status.PAID:
            raise ValidationError(
                "Issue and settle the final checkout invoice before completing checkout."
            )


class BookingAdminForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = ("status",)

    def clean_status(self):
        new_status = self.cleaned_data["status"]
        if not self.instance.pk:
            return new_status
        booking = Booking.objects.get(pk=self.instance.pk)
        try:
            validate_status_change(booking, new_status)
        except ValidationError as error:
            raise forms.ValidationError(error.messages) from error
        return new_status


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    form = BookingAdminForm
    list_display = ("id", "customer", "room", "check_in", "check_out", "status", "total_price")
    list_filter = (
        "status",
        ("check_in", admin.DateFieldListFilter),
        ("check_out", admin.DateFieldListFilter),
        ("room", admin.RelatedOnlyFieldListFilter),
        ("customer", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = (
        "customer__username",
        "customer__first_name",
        "customer__last_name",
        "customer__email",
        "room__room_number",
        "room__room_type__name",
    )
    date_hierarchy = "check_in"
    list_select_related = ("customer", "room", "room__room_type")
    fields = (
        "customer", "room", "check_in", "check_out", "guests", "total_price", "created_at", "status"
    )
    readonly_fields = (
        "customer", "room", "check_in", "check_out", "guests", "total_price", "created_at"
    )
    actions = None

    def _can_manage_bookings(self, request):
        return request.user.is_superuser or (
            request.user.is_staff
            and request.user.role in {User.Role.RECEPTIONIST, User.Role.MANAGER}
        )

    def has_module_permission(self, request):
        return self._can_manage_bookings(request)

    def has_view_permission(self, request, obj=None):
        return self._can_manage_bookings(request)

    def has_change_permission(self, request, obj=None):
        return self._can_manage_bookings(request)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "customer", "room", "room__room_type"
        )

    def save_model(self, request, obj, form, change):
        if not change:
            raise ValidationError("Bookings must be created through the validated booking flow.")
        with transaction.atomic():
            current = Booking.objects.select_for_update().get(pk=obj.pk)
            Room.objects.select_for_update().get(pk=current.room_id)
            validate_status_change(current, obj.status)
            current.transition_to(obj.status)
            obj.status = current.status
