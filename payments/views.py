import json
import logging
from datetime import timedelta
from decimal import Decimal

import razorpay
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from bookings.models import Booking
from operations.billing import calculate_invoice_amounts
from operations.models import CheckoutInvoice
from .models import Payment, Receipt

logger = logging.getLogger(__name__)


def get_razorpay_client():
    key_id = settings.RAZORPAY_KEY_ID
    key_secret = settings.RAZORPAY_KEY_SECRET
    if not key_id or not key_secret:
        raise ValueError("Razorpay test credentials are not configured.")
    if not key_id.startswith("rzp_test_"):
        raise ValueError("Only Razorpay TEST MODE credentials are permitted.")
    return razorpay.Client(auth=(key_id, key_secret))


def _amount_in_paise(amount):
    return int((Decimal(amount) * Decimal("100")).quantize(Decimal("1")))


def _matches_amount(value, expected_amount):
    try:
        return int(value) == expected_amount
    except (TypeError, ValueError):
        return False


def _safe_order_error_category(exc):
    """Return an allowlisted diagnostic label without logging provider text."""
    description = str(exc).casefold()
    if any(term in description for term in (
        "auth", "credential", "api key", "key_id", "unauthorized",
    )):
        return "authentication_rejected"
    if "amount" in description or "currency" in description:
        return "order_amount_or_currency_rejected"
    if "receipt" in description:
        return "receipt_rejected"
    if isinstance(exc, razorpay.errors.BadRequestError):
        return "razorpay_bad_request"
    if type(exc).__module__.startswith("requests"):
        return "network_error"
    return "sdk_or_unexpected_error"


def _payment_matches_order(payment, provider_order, provider_payment, payment_id):
    """Check independently fetched Razorpay entities against our local order."""
    if payment.kind == Payment.Kind.SERVICE:
        if (
            not payment.invoice_id
            or payment.invoice.booking_id != payment.booking_id
            or payment.invoice.status not in {
                CheckoutInvoice.Status.ISSUED,
                CheckoutInvoice.Status.PARTIALLY_PAID,
            }
            or payment.amount > calculate_invoice_amounts(payment.booking, payment.invoice)["outstanding"]
        ):
            return False
    expected_amount = _amount_in_paise(payment.amount)
    return (
        provider_order.get("id") == payment.provider_order_id
        and _matches_amount(provider_order.get("amount"), expected_amount)
        and provider_order.get("currency") == "INR"
        and provider_payment.get("id") == payment_id
        and provider_payment.get("status") == "captured"
        and provider_payment.get("order_id") == payment.provider_order_id
        and _matches_amount(provider_payment.get("amount"), expected_amount)
        and provider_payment.get("currency") == "INR"
    )


def _receipt_snapshot(payment, provider_payment):
    booking = payment.booking
    if payment.kind == Payment.Kind.SERVICE:
        invoice = payment.invoice
        customer = booking.customer
        return {
            "receipt_type": "SERVICE",
            "hotel_name": getattr(settings, "HOTEL_NAME", "Serene Valley Hotel & Resort"),
            "hotel_address": getattr(settings, "HOTEL_ADDRESS", ""),
            "hotel_email": getattr(settings, "HOTEL_EMAIL", ""),
            "hotel_phone": getattr(settings, "HOTEL_CONTACT_PHONE", ""),
            "hotel_gstin": getattr(settings, "HOTEL_GSTIN", ""),
            "customer_name": customer.get_full_name().strip() or customer.get_username(),
            "customer_email": customer.email,
            "customer_phone": getattr(customer, "phone", ""),
            "booking_id": booking.pk,
            "invoice_number": invoice.invoice_number,
            "amount": str(payment.amount),
            "amount_received": str(payment.amount),
            "currency": "INR",
            "payment_method": provider_payment.get("method", ""),
            "provider_payment_id": provider_payment["id"],
            "provider_order_id": payment.provider_order_id,
        }
    room = booking.room
    room_type = room.room_type
    nights = (booking.check_out - booking.check_in).days
    customer = booking.customer
    return {
        "receipt_type": "ROOM",
        "hotel_name": getattr(settings, "HOTEL_NAME", "Serene Valley Hotel & Resort"),
        "hotel_address": getattr(settings, "HOTEL_ADDRESS", ""),
        "hotel_email": getattr(settings, "HOTEL_EMAIL", ""),
        "hotel_phone": getattr(settings, "HOTEL_CONTACT_PHONE", ""),
        "hotel_gstin": getattr(settings, "HOTEL_GSTIN", ""),
        "customer_name": customer.get_full_name().strip() or customer.get_username(),
        "customer_email": customer.email,
        "customer_phone": getattr(customer, "phone", ""),
        "booking_id": booking.pk,
        "room_number": room.room_number,
        "room_type": room_type.name,
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "nights": nights,
        "guests": booking.guests,
        # Booking stores a fixed total but no historical rate snapshot. Derive
        # the effective rate from that trusted total, not today's room price.
        "nightly_rate": str((payment.amount / nights).quantize(Decimal("0.01"))) if nights else "0.00",
        "amount": str(payment.amount),
        "currency": "INR",
        "payment_method": provider_payment.get("method", ""),
        "provider_payment_id": provider_payment["id"],
        "provider_order_id": payment.provider_order_id,
    }


def _record_captured_payment(payment, provider_order, provider_payment, payment_id):
    if not _payment_matches_order(payment, provider_order, provider_payment, payment_id):
        return False
    if payment.status == Payment.Status.SUCCESS:
        if payment.provider_payment_id != payment_id:
            return False
        Receipt.objects.get_or_create(
            payment=payment,
            defaults={"details": _receipt_snapshot(payment, provider_payment)},
        )
        return True
    if payment.status != Payment.Status.PENDING:
        return False

    # A captured charge is recorded truthfully. Other local pending orders are
    # closed so they cannot be offered for another checkout for this booking.
    pending = Payment.objects.filter(
        booking_id=payment.booking_id,
        kind=payment.kind,
        status=Payment.Status.PENDING,
    ).exclude(pk=payment.pk)
    if payment.kind == Payment.Kind.SERVICE:
        pending = pending.filter(invoice_id=payment.invoice_id)
    pending.update(status=Payment.Status.FAILED)
    payment.provider_payment_id = payment_id
    payment.status = Payment.Status.SUCCESS
    payment.save(update_fields=["provider_payment_id", "status"])
    Receipt.objects.get_or_create(
        payment=payment,
        defaults={"details": _receipt_snapshot(payment, provider_payment)},
    )
    if payment.kind == Payment.Kind.SERVICE:
        from operations.billing import update_invoice_status
        update_invoice_status(payment.invoice)
    return True


@login_required
def receipt_detail(request, receipt_id):
    receipt = get_object_or_404(
        Receipt.objects.select_related("payment", "payment__booking"),
        pk=receipt_id,
        payment__booking__customer=request.user,
        payment__status=Payment.Status.SUCCESS,
    )
    return render(request, "payments/receipt.html", {"receipt": receipt})


@login_required
def receipt_pdf(request, receipt_id):
    receipt = get_object_or_404(
        Receipt.objects.select_related("payment", "payment__booking"),
        pk=receipt_id,
        payment__booking__customer=request.user,
        payment__status=Payment.Status.SUCCESS,
    )
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    data = receipt.details
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=22 * mm, leftMargin=22 * mm,
        topMargin=22 * mm, bottomMargin=22 * mm,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReceiptTitle", parent=styles["Title"], alignment=TA_CENTER,
        textColor=colors.HexColor("#173b32"), spaceAfter=8 * mm,
    ))
    story = [
        Paragraph(escape(data["hotel_name"]), styles["ReceiptTitle"]),
        Paragraph("PAYMENT RECEIPT", styles["Heading2"]),
        Paragraph(f"Receipt: {escape(receipt.receipt_number)} &nbsp; | &nbsp; Issued: {receipt.issued_at:%d %b %Y}", styles["BodyText"]),
        Spacer(1, 6 * mm),
    ]
    hotel_lines = [x for x in (
        data.get("hotel_address"), data.get("hotel_email"), data.get("hotel_phone"),
        f"GSTIN: {data['hotel_gstin']}" if data.get("hotel_gstin") else "",
    ) if x]
    if hotel_lines:
        story.append(Paragraph("<br/>".join(escape(line) for line in hotel_lines), styles["BodyText"]))
        story.append(Spacer(1, 5 * mm))
    if data.get("receipt_type") == "SERVICE":
        rows = [
            ["Customer", data["customer_name"]],
            ["Email", data.get("customer_email") or "—"],
            ["Phone", data.get("customer_phone") or "—"],
            ["Reservation", f"#{data['booking_id']}"],
            ["Final invoice", data["invoice_number"]],
            ["Payment method", data.get("payment_method") or "Razorpay"],
            ["Razorpay order reference", data["provider_order_id"]],
            ["Payment reference", data["provider_payment_id"]],
            ["Amount received", f"INR {data['amount_received']}"],
        ]
    else:
        rows = [
            ["Customer", data["customer_name"]],
            ["Email", data.get("customer_email") or "—"],
            ["Phone", data.get("customer_phone") or "—"],
            ["Reservation", f"#{data['booking_id']}"],
            ["Room", f"{data['room_type']} · {data['room_number']}"],
            ["Stay", f"{data['check_in']} to {data['check_out']} ({data['nights']} nights)"],
            ["Guests", str(data["guests"])],
            ["Rate per night", f"INR {data['nightly_rate']}"],
            ["Payment method", data.get("payment_method") or "Razorpay"],
            ["Razorpay order reference", data["provider_order_id"]],
            ["Payment reference", data["provider_payment_id"]],
            ["Room charges", f"INR {data['amount']}"],
            ["TOTAL PAID", f"INR {data['amount']}"],
        ]
    rows = [[escape(str(cell)) for cell in row] for row in rows]
    table = Table(rows, colWidths=[48 * mm, 112 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f4ef")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#26332d")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d7ded8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story.extend([table, Spacer(1, 8 * mm), Paragraph("Thank you for choosing Serene Valley Hotel &amp; Resort.", styles["BodyText"])])
    document.build(story)
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{receipt.receipt_number}.pdf"'
    return response


@login_required
@require_POST
def create_payment(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.select_related("room", "room__room_type"),
        id=booking_id,
        customer=request.user,
    )
    if booking.status != Booking.Status.CONFIRMED:
        messages.error(request, "Only confirmed bookings are eligible for payment.")
        return redirect("bookings:detail", booking_id=booking.id)
    if Payment.objects.filter(booking=booking, kind=Payment.Kind.ROOM, status=Payment.Status.SUCCESS).exists():
        messages.info(request, "This booking has already been paid.")
        return redirect("bookings:detail", booking_id=booking.id)

    created_payment = False
    with transaction.atomic():
        pending_payment = Payment.objects.select_for_update().filter(
            booking=booking,
            kind=Payment.Kind.ROOM,
            status=Payment.Status.PENDING,
        ).first()
        if pending_payment is None:
            try:
                # A nested savepoint lets a concurrent request safely recover
                # the row protected by the conditional unique constraint.
                with transaction.atomic():
                    pending_payment = Payment.objects.create(
                        booking=booking,
                        kind=Payment.Kind.ROOM,
                        amount=booking.total_price,
                        status=Payment.Status.PENDING,
                    )
                created_payment = True
            except IntegrityError:
                pending_payment = Payment.objects.filter(
                    booking=booking,
                    kind=Payment.Kind.ROOM,
                    status=Payment.Status.PENDING,
                ).first()

    if pending_payment is None:
        messages.error(request, "Payment setup is busy. Please try again shortly.")
        return redirect("bookings:detail", booking_id=booking.id)
    if pending_payment.provider_order_id:
        return redirect("payments:checkout", payment_id=pending_payment.id)
    if not created_payment:
        # Do not create a second order while the first request is contacting
        # Razorpay. A stale empty row is recoverable after a process crash.
        if pending_payment.created_at > timezone.now() - timedelta(minutes=10):
            messages.info(request, "Your payment order is being prepared. Please retry shortly.")
            return HttpResponse("Payment order is being prepared. Please retry shortly.", status=409)
        pending_payment.status = Payment.Status.FAILED
        pending_payment.save(update_fields=["status"])
        with transaction.atomic():
            pending_payment = Payment.objects.create(
                booking=booking,
                kind=Payment.Kind.ROOM,
                amount=booking.total_price,
                status=Payment.Status.PENDING,
            )
        created_payment = True

    try:
        client = get_razorpay_client()
        amount_paise = _amount_in_paise(pending_payment.amount)
        order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"payment_{pending_payment.id}",
        })
        if (
            not order.get("id")
            or int(order.get("amount", -1)) != amount_paise
            or order.get("currency") != "INR"
        ):
            raise ValueError("Razorpay returned an order that does not match the booking.")
    except Exception as exc:
        # Do not log provider response bodies or exception messages: they may
        # contain payment or credential-related data. The type is enough to
        # distinguish local configuration, SDK, and network failures.
        logger.error(
            "Razorpay order setup failed (exception_type=%s, category=%s)",
            type(exc).__name__,
            _safe_order_error_category(exc),
        )
        pending_payment.status = Payment.Status.FAILED
        pending_payment.save(update_fields=["status"])
        messages.error(request, "Payment setup failed. Please try again later.")
        return redirect("bookings:detail", booking_id=booking.id)

    pending_payment.provider_order_id = order["id"]
    pending_payment.save(update_fields=["provider_order_id"])

    return redirect("payments:checkout", payment_id=pending_payment.id)


@login_required
@require_POST
def create_invoice_payment(request, invoice_id):
    invoice = get_object_or_404(
        CheckoutInvoice.objects.select_related("booking"),
        pk=invoice_id,
        booking__customer=request.user,
    )
    if invoice.status == CheckoutInvoice.Status.DRAFT:
        messages.error(request, "This invoice has not been issued yet.")
        return redirect("operations:invoice", invoice_id=invoice.pk)

    with transaction.atomic():
        invoice = CheckoutInvoice.objects.select_for_update().select_related("booking").get(pk=invoice.pk)
        amounts = calculate_invoice_amounts(invoice.booking, invoice)
        due = amounts["outstanding"]
        if due <= 0:
            messages.info(request, "There is no outstanding invoice balance to pay.")
            return redirect("operations:invoice", invoice_id=invoice.pk)
        pending = Payment.objects.select_for_update().filter(
            invoice=invoice, kind=Payment.Kind.SERVICE, status=Payment.Status.PENDING
        ).first()
        if pending and pending.amount == due and pending.provider_order_id:
            return redirect("payments:checkout", payment_id=pending.pk)
        if pending and not pending.provider_order_id and pending.created_at > timezone.now() - timedelta(minutes=10):
            return HttpResponse("Your invoice payment order is being prepared. Please retry shortly.", status=409)
        if pending:
            pending.status = Payment.Status.FAILED
            pending.save(update_fields=["status"])
        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    booking=invoice.booking,
                    invoice=invoice,
                    kind=Payment.Kind.SERVICE,
                    amount=due,
                    status=Payment.Status.PENDING,
                )
        except IntegrityError:
            pending = Payment.objects.filter(
                invoice=invoice, kind=Payment.Kind.SERVICE, status=Payment.Status.PENDING
            ).first()
            if pending and pending.provider_order_id:
                return redirect("payments:checkout", payment_id=pending.pk)
            return HttpResponse("Your invoice payment is being prepared. Please retry shortly.", status=409)

    try:
        client = get_razorpay_client()
        amount_paise = _amount_in_paise(payment.amount)
        order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"invoice_{invoice.pk}_payment_{payment.pk}",
        })
        if (
            not order.get("id")
            or int(order.get("amount", -1)) != amount_paise
            or order.get("currency") != "INR"
        ):
            raise ValueError("Razorpay returned an order that does not match the invoice balance.")
    except Exception as exc:
        logger.error(
            "Razorpay invoice order setup failed (exception_type=%s, category=%s)",
            type(exc).__name__, _safe_order_error_category(exc),
        )
        payment.status = Payment.Status.FAILED
        payment.save(update_fields=["status"])
        messages.error(request, "Invoice payment setup failed. Please try again later.")
        return redirect("operations:invoice", invoice_id=invoice.pk)

    payment.provider_order_id = order["id"]
    payment.save(update_fields=["provider_order_id"])
    return redirect("payments:checkout", payment_id=payment.pk)


@login_required
def checkout(request, payment_id):
    payment = get_object_or_404(
        Payment.objects.select_related("booking", "booking__room", "invoice"),
        id=payment_id,
        booking__customer=request.user,
    )
    if payment.status != Payment.Status.PENDING:
        return HttpResponse("This payment is no longer pending.", status=400)
    if payment.kind == Payment.Kind.SERVICE:
        amounts = calculate_invoice_amounts(payment.booking, payment.invoice)
        if (
            payment.invoice.status not in {CheckoutInvoice.Status.ISSUED, CheckoutInvoice.Status.PARTIALLY_PAID}
            or payment.amount != amounts["outstanding"]
        ):
            return HttpResponse("This invoice payment order is no longer eligible. Return to the invoice for the current balance.", status=400)
    if not payment.provider_order_id:
        return HttpResponse("A payment order has not been created.", status=400)
    if not settings.RAZORPAY_KEY_ID.startswith("rzp_test_"):
        return HttpResponse("Razorpay TEST MODE is not configured.", status=503)

    return render(request, "payments/checkout.html", {
        "payment": payment,
        "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        "amount_paise": int(payment.amount * Decimal("100")),
    })


@login_required
@require_POST
def verify_payment(request, payment_id):
    razorpay_payment_id = request.POST.get("razorpay_payment_id", "")
    razorpay_order_id = request.POST.get("razorpay_order_id", "")
    razorpay_signature = request.POST.get("razorpay_signature", "")
    if not all((razorpay_payment_id, razorpay_order_id, razorpay_signature)):
        return HttpResponseBadRequest("Missing payment verification details.")

    with transaction.atomic():
        payment = get_object_or_404(
            Payment.objects.select_for_update().select_related("booking", "invoice"),
            id=payment_id,
            booking__customer=request.user,
        )
        if payment.status == Payment.Status.SUCCESS:
            if payment.kind == Payment.Kind.SERVICE:
                return redirect("operations:invoice", invoice_id=payment.invoice_id)
            return redirect("bookings:detail", booking_id=payment.booking_id)
        if payment.status != Payment.Status.PENDING:
            return HttpResponse("This payment is no longer pending.", status=400)
        if razorpay_order_id != payment.provider_order_id:
            return HttpResponseBadRequest("Payment order does not match.")

        try:
            client = get_razorpay_client()
            client.utility.verify_payment_signature({
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            })
            provider_order = client.order.fetch(razorpay_order_id)
            provider_payment = client.payment.fetch(razorpay_payment_id)
        except razorpay.errors.SignatureVerificationError:
            return HttpResponseBadRequest("Payment signature verification failed.")
        except Exception:
            return HttpResponse("Could not confirm payment status. Please retry.", status=502)

        # Re-read under a row lock before writing, since the webhook can race
        # this browser callback.
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if not _record_captured_payment(
            payment, provider_order, provider_payment, razorpay_payment_id
        ):
            return HttpResponse("Payment is not captured or does not match this order.", status=400)

    messages.success(request, "Payment received and verified.")
    if payment.kind == Payment.Kind.SERVICE:
        return redirect("operations:invoice", invoice_id=payment.invoice_id)
    return redirect("bookings:detail", booking_id=payment.booking_id)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not secret or not signature:
        return HttpResponseBadRequest("Webhook verification is not configured.")

    try:
        client = get_razorpay_client()
    except ValueError:
        return HttpResponse("Razorpay TEST MODE is not configured.", status=503)
    try:
        client.utility.verify_webhook_signature(
            request.body.decode("utf-8"), signature, secret
        )
    except Exception:
        return HttpResponseBadRequest("Invalid webhook signature.")

    try:
        event = json.loads(request.body)
        if event.get("event") not in {"payment.captured", "payment.failed"}:
            return HttpResponse(status=200)
        entity = event["payload"]["payment"]["entity"]
        order_id = entity["order_id"]
    except (KeyError, TypeError, ValueError):
        return HttpResponseBadRequest("Invalid webhook payload.")

    if event.get("event") == "payment.failed":
        # A failed attempt does not invalidate the Razorpay order; the customer
        # can safely retry on the same pending order. Abandoned orders likewise
        # remain pending and are reused on the next visit.
        return HttpResponse(status=200)

    payment = Payment.objects.select_related("booking").filter(
        provider_order_id=order_id,
    ).first()
    if payment is None:
        return HttpResponse(status=200)
    if payment.status == Payment.Status.SUCCESS:
        return HttpResponse(status=200)
    if payment.status != Payment.Status.PENDING:
        return HttpResponse(status=200)

    try:
        client = get_razorpay_client()
        provider_order = client.order.fetch(order_id)
        provider_payment = client.payment.fetch(entity["id"])
    except Exception:
        # Non-2xx causes Razorpay to retry delivery; never acknowledge an event
        # whose captured state we could not independently confirm.
        return HttpResponse("Could not confirm payment with Razorpay.", status=502)

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == Payment.Status.SUCCESS:
            return HttpResponse(status=200)
        if not _record_captured_payment(
            payment, provider_order, provider_payment, entity["id"]
        ):
            return HttpResponseBadRequest("Captured payment does not match the local order.")

    return HttpResponse(status=200)
