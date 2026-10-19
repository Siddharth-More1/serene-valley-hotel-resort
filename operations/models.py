from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from bookings.models import Booking
from properties.models import Room, Service


class HousekeepingTask(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"

    room = models.ForeignKey(
        Room,
        on_delete=models.PROTECT,
        related_name="housekeeping_tasks",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="housekeeping_tasks",
    )
    description = models.CharField(max_length=200)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.room} - {self.description}"


class ServiceRequest(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        APPROVED = "APPROVED", "Approved"
        COMPLETED = "COMPLETED", "Completed"
        REJECTED = "REJECTED", "Rejected"

    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="service_requests")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="guest_requests")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    price_at_request = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    customer_note = models.CharField(max_length=500, blank=True)
    staff_note = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-requested_at",)
        indexes = [models.Index(fields=("booking", "status"))]

    def __str__(self):
        return f"{self.service.name} for booking #{self.booking_id} ({self.status})"


class FolioItem(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        REVERSED = "REVERSED", "Reversed"

    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="folio_items")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="folio_items")
    request = models.OneToOneField(
        ServiceRequest, null=True, blank=True, on_delete=models.PROTECT,
        related_name="folio_item",
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.COMPLETED)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="folio_items_created"
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="folio_items_confirmed"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="folio_items_reversed",
    )
    reversal_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("created_at", "pk")
        indexes = [models.Index(fields=("booking", "status"))]

    def __str__(self):
        return f"{self.service.name} × {self.quantity} for booking #{self.booking_id}"


class CheckoutInvoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ISSUED = "ISSUED", "Issued"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        PAID = "PAID", "Paid"
        OVERPAID = "OVERPAID", "Credit due"

    booking = models.OneToOneField(Booking, on_delete=models.PROTECT, related_name="checkout_invoice")
    invoice_number = models.CharField(max_length=40, unique=True, null=True, blank=True)
    room_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    service_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    issued_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.invoice_number or f"Draft invoice for booking #{self.booking_id}"


class InvoiceLine(models.Model):
    class Kind(models.TextChoices):
        ROOM = "ROOM", "Room charges"
        SERVICE = "SERVICE", "Service"

    invoice = models.ForeignKey(CheckoutInvoice, on_delete=models.PROTECT, related_name="lines")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    description = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    folio_item = models.ForeignKey(
        FolioItem, null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines"
    )

    class Meta:
        ordering = ("pk",)
        constraints = [
            models.UniqueConstraint(fields=("invoice", "folio_item"), name="uniq_invoice_folio_line"),
        ]

    def save(self, *args, **kwargs):
        if self.invoice_id and self.invoice.status != CheckoutInvoice.Status.DRAFT:
            raise ValueError("Issued invoice lines are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.invoice.status != CheckoutInvoice.Status.DRAFT:
            raise ValueError("Issued invoice lines cannot be deleted.")
        return super().delete(*args, **kwargs)
