from django.db import models
from django.db.models import Q
from bookings.models import Booking


class Payment(models.Model):
    class Kind(models.TextChoices):
        ROOM = "ROOM", "Room payment"
        SERVICE = "SERVICE", "Checkout invoice payment"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"

    booking = models.ForeignKey(
        Booking,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.ROOM)
    invoice = models.ForeignKey(
        "operations.CheckoutInvoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="service_payments",
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    provider_order_id = models.CharField(
        max_length=100,
        blank=True,
    )
    provider_payment_id = models.CharField(
        max_length=100,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["booking"],
                condition=Q(kind="ROOM", status__in=["PENDING", "SUCCESS"]),
                name="uniq_active_room_payment_per_booking",
            ),
            models.UniqueConstraint(
                fields=["invoice"],
                condition=Q(kind="SERVICE", status="PENDING"),
                name="uniq_pending_invoice_payment",
            ),
            models.UniqueConstraint(
                fields=["provider_order_id"],
                condition=~Q(provider_order_id=""),
                name="uniq_nonempty_provider_order_id",
            ),
            models.UniqueConstraint(
                fields=["provider_payment_id"],
                condition=~Q(provider_payment_id=""),
                name="uniq_nonempty_provider_payment_id",
            ),
        ]

    def __str__(self):
        return f"Payment #{self.pk} - {self.status}"


class Receipt(models.Model):
    """Immutable billing snapshot issued once for a verified payment."""

    payment = models.OneToOneField(
        Payment,
        on_delete=models.PROTECT,
        related_name="receipt",
    )
    issued_at = models.DateTimeField(auto_now_add=True)
    details = models.JSONField()

    @property
    def receipt_number(self):
        # The immutable primary key and issuance year give each invoice a
        # stable, unique number without a race-prone counter.
        return f"SVHR-{self.issued_at.year}-{self.pk:06d}"

    def __str__(self):
        return self.receipt_number
