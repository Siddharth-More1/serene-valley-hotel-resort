from decimal import Decimal

from django.db.models import Sum

from payments.models import Payment
from .models import CheckoutInvoice, FolioItem


ZERO = Decimal("0.00")


def calculate_invoice_amounts(booking, invoice=None):
    """Central source of truth for stay totals and retained verified payments."""
    if invoice is not None and invoice.pk and invoice.status != CheckoutInvoice.Status.DRAFT:
        room_total = invoice.room_charges
        services = invoice.service_charges
        total = invoice.total_charges
    else:
        services = FolioItem.objects.filter(
            booking=booking, status=FolioItem.Status.COMPLETED
        ).aggregate(total=Sum("line_total"))["total"] or ZERO
        room_total = booking.total_price
        total = room_total + services
    room_paid = Payment.objects.filter(
        booking=booking, kind=Payment.Kind.ROOM, status=Payment.Status.SUCCESS
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    service_paid = ZERO
    if invoice is not None and invoice.pk:
        service_paid = Payment.objects.filter(
            invoice=invoice,
            kind=Payment.Kind.SERVICE,
            status=Payment.Status.SUCCESS,
        ).aggregate(total=Sum("amount"))["total"] or ZERO
    paid = room_paid + service_paid
    return {
        "room_charges": room_total,
        "service_charges": services,
        "total_charges": total,
        "room_paid": room_paid,
        "service_paid": service_paid,
        "total_paid": paid,
        "outstanding": max(total - paid, ZERO),
        "credit": max(paid - total, ZERO),
    }


def update_invoice_status(invoice):
    amounts = calculate_invoice_amounts(invoice.booking, invoice)
    if amounts["credit"] > ZERO:
        invoice.status = CheckoutInvoice.Status.OVERPAID
    elif amounts["outstanding"] == ZERO:
        invoice.status = CheckoutInvoice.Status.PAID
    elif amounts["total_paid"] > ZERO:
        invoice.status = CheckoutInvoice.Status.PARTIALLY_PAID
    else:
        invoice.status = CheckoutInvoice.Status.ISSUED
    invoice.save(update_fields=["status"])
    return amounts
