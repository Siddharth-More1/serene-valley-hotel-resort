from django.db import models

from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from properties.models import Room


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        CHECKED_IN = "CHECKED_IN", "Checked In"
        CHECKED_OUT = "CHECKED_OUT", "Checked Out"
        CANCELLED = "CANCELLED", "Cancelled"

    STATUS_TRANSITIONS = {
        Status.PENDING: {Status.CONFIRMED, Status.CANCELLED},
        Status.CONFIRMED: {Status.CHECKED_IN, Status.CANCELLED},
        Status.CHECKED_IN: {Status.CHECKED_OUT},
        Status.CHECKED_OUT: set(),
        Status.CANCELLED: set(),
    }

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    room = models.ForeignKey(
        Room,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    check_in = models.DateField()
    check_out = models.DateField()
    guests = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    total_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.check_in and self.check_out:
            if self.check_out <= self.check_in:
                raise ValidationError(
                    "Check-out must be after check-in."
                )

        if self.guests and self.room_id:
            if self.guests > self.room.room_type.capacity:
                raise ValidationError(
                    "Guest count exceeds room capacity."
                )

    def can_transition_to(self, new_status):
        return new_status == self.status or new_status in self.STATUS_TRANSITIONS.get(self.status, set())

    def transition_to(self, new_status, *, save=True):
        if not self.can_transition_to(new_status):
            status_label = dict(self.Status.choices).get(new_status, new_status)
            raise ValidationError(
                f"A booking cannot move from {self.get_status_display()} to "
                f"{status_label}."
            )
        if new_status == self.Status.CHECKED_OUT:
            from operations.models import CheckoutInvoice
            invoice = getattr(self, "checkout_invoice", None)
            if invoice is None or invoice.status != CheckoutInvoice.Status.PAID:
                raise ValidationError(
                    "A fully paid final checkout invoice is required before checkout can complete."
                )
        self.status = new_status
        if save:
            self.save(update_fields=["status"])

    def __str__(self):
        return f"Booking #{self.pk} - {self.customer}"
