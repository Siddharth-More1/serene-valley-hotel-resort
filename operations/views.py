from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.validators import MinValueValidator
from django.db import transaction
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User
from bookings.models import Booking
from payments.models import Payment, Receipt
from properties.models import Room, Service
from .billing import calculate_invoice_amounts, update_invoice_status
from .forms import ServiceRequestForm, StaffUsageForm
from .models import CheckoutInvoice, FolioItem, InvoiceLine, ServiceRequest


def _is_billing_staff(user):
    return user.is_superuser or (
        user.is_staff and user.role in {User.Role.RECEPTIONIST, User.Role.MANAGER}
    )


def _require_billing_staff(user):
    if not _is_billing_staff(user):
        raise PermissionDenied


def _booking_is_in_service_window(booking):
    today = timezone.localdate()
    return (
        booking.status == Booking.Status.CHECKED_IN
        and today <= booking.check_out
    )


@login_required
def manager_dashboard(request):
    user = request.user
    if not user.is_superuser and not (
        user.is_staff and user.role == User.Role.MANAGER
    ):
        raise PermissionDenied

    today = timezone.localdate()
    status_counts = {
        row["status"]: row["count"]
        for row in Booking.objects.values("status").annotate(count=Count("pk"))
    }
    active_rooms = Room.objects.filter(status=Room.Status.AVAILABLE).count()
    occupied_rooms = Room.objects.filter(
        status=Room.Status.AVAILABLE,
        bookings__status=Booking.Status.CHECKED_IN,
        bookings__check_in__lte=today,
        bookings__check_out__gt=today,
    ).distinct().count()
    occupancy_percent = (
        round(occupied_rooms * 100 / active_rooms, 1) if active_rooms else None
    )
    verified_revenue = Payment.objects.filter(
        status=Payment.Status.SUCCESS
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    recent_bookings = Booking.objects.select_related(
        "room", "room__room_type"
    ).order_by("-created_at")[:10]

    return render(request, "operations/manager_dashboard.html", {
        "total_bookings": sum(status_counts.values()),
        "pending_bookings": status_counts.get(Booking.Status.PENDING, 0),
        "confirmed_bookings": status_counts.get(Booking.Status.CONFIRMED, 0),
        "checked_in_bookings": status_counts.get(Booking.Status.CHECKED_IN, 0),
        "checked_out_bookings": status_counts.get(Booking.Status.CHECKED_OUT, 0),
        "cancelled_bookings": status_counts.get(Booking.Status.CANCELLED, 0),
        "active_rooms": active_rooms,
        "occupied_rooms": occupied_rooms,
        "occupancy_percent": occupancy_percent,
        "occupancy_calculable": active_rooms > 0,
        "verified_revenue": verified_revenue,
        "recent_bookings": recent_bookings,
    })


@login_required
def service_request(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.select_related("room", "room__room_type"),
        pk=booking_id, customer=request.user,
    )
    existing_invoice = CheckoutInvoice.objects.filter(booking=booking).first()
    can_request = _booking_is_in_service_window(booking) and (
        existing_invoice is None or existing_invoice.status == CheckoutInvoice.Status.DRAFT
    )
    form = ServiceRequestForm(request.POST or None)
    if request.method == "POST":
        if not can_request:
            messages.error(request, "Services can be requested only during your checked-in stay.")
        elif form.is_valid():
            with transaction.atomic():
                locked_booking = Booking.objects.select_for_update().get(pk=booking.pk)
                locked_invoice = CheckoutInvoice.objects.filter(booking=locked_booking).first()
                if not _booking_is_in_service_window(locked_booking) or (
                    locked_invoice and locked_invoice.status != CheckoutInvoice.Status.DRAFT
                ):
                    messages.error(request, "This stay is no longer accepting service requests.")
                else:
                    service = Service.objects.select_for_update().get(pk=form.cleaned_data["service"].pk)
                    if not service.is_active:
                        form.add_error("service", "This service is no longer available.")
                    else:
                        request_obj = form.save(commit=False)
                        request_obj.booking = locked_booking
                        request_obj.price_at_request = service.price
                        request_obj.save()
                        messages.success(request, "Your service request was sent to the hotel team.")
                        return redirect("operations:service_request", booking_id=booking.pk)
    requests = booking.service_requests.select_related("service").all()
    return render(request, "operations/service_request.html", {
        "booking": booking,
        "form": form,
        "requests": requests,
        "services": Service.objects.filter(is_active=True).order_by("name"),
        "can_request": can_request,
    })


@login_required
def staff_service_board(request):
    _require_billing_staff(request.user)
    requests = ServiceRequest.objects.select_related(
        "booking", "booking__customer", "service", "booking__room"
    ).exclude(status__in=[ServiceRequest.Status.COMPLETED, ServiceRequest.Status.REJECTED])
    checked_in_bookings = Booking.objects.filter(
        status=Booking.Status.CHECKED_IN
    ).select_related("customer", "room", "room__room_type").order_by("check_out")
    return render(request, "operations/service_board.html", {
        "requests": requests,
        "checked_in_bookings": checked_in_bookings,
        "services": Service.objects.filter(is_active=True).order_by("name"),
    })


@login_required
@require_POST
def update_service_request(request, request_id):
    _require_billing_staff(request.user)
    with transaction.atomic():
        service_request = get_object_or_404(
            ServiceRequest.objects.select_for_update().select_related("booking", "service"),
            pk=request_id,
        )
        action = request.POST.get("action")
        note = request.POST.get("staff_note", "").strip()[:500]
        if action == "approve" and service_request.status == ServiceRequest.Status.REQUESTED:
            service_request.status = ServiceRequest.Status.APPROVED
        elif action == "reject" and service_request.status in {ServiceRequest.Status.REQUESTED, ServiceRequest.Status.APPROVED}:
            service_request.status = ServiceRequest.Status.REJECTED
        elif action == "complete" and service_request.status in {ServiceRequest.Status.REQUESTED, ServiceRequest.Status.APPROVED}:
            booking = Booking.objects.select_for_update().get(pk=service_request.booking_id)
            if not _booking_is_in_service_window(booking):
                messages.error(request, "Service usage can be confirmed only during the checked-in stay.")
                return redirect("operations:service_board")
            if hasattr(booking, "checkout_invoice") and booking.checkout_invoice.status != CheckoutInvoice.Status.DRAFT:
                messages.error(request, "This invoice is finalized; service charges cannot be added.")
                return redirect("operations:service_board")
            FolioItem.objects.create(
                booking=booking,
                service=service_request.service,
                request=service_request,
                quantity=service_request.quantity,
                unit_price=service_request.price_at_request,
                line_total=service_request.price_at_request * service_request.quantity,
                note=note or service_request.customer_note,
                created_by=request.user,
                confirmed_by=request.user,
            )
            service_request.status = ServiceRequest.Status.COMPLETED
        else:
            messages.error(request, "That request can no longer be changed to the selected status.")
            return redirect("operations:service_board")
        service_request.staff_note = note
        service_request.save(update_fields=["status", "staff_note", "updated_at"])
        messages.success(request, "Service request updated.")
    return redirect("operations:service_board")


@login_required
@require_POST
def add_folio_item(request, booking_id):
    _require_billing_staff(request.user)
    booking = get_object_or_404(Booking, pk=booking_id)
    form = StaffUsageForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose an available service and a valid quantity.")
        return redirect("operations:service_board")
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
        if not _booking_is_in_service_window(booking):
            messages.error(request, "Direct service usage can be recorded only during a checked-in stay.")
        elif hasattr(booking, "checkout_invoice") and booking.checkout_invoice.status != CheckoutInvoice.Status.DRAFT:
            messages.error(request, "This invoice is finalized; add-on charges are locked.")
        else:
            service = Service.objects.select_for_update().get(pk=form.cleaned_data["service"].pk)
            if not service.is_active:
                messages.error(request, "This service is no longer active.")
            else:
                quantity = form.cleaned_data["quantity"]
                FolioItem.objects.create(
                    booking=booking,
                    service=service,
                    quantity=quantity,
                    unit_price=service.price,
                    line_total=service.price * quantity,
                    note=form.cleaned_data["note"],
                    created_by=request.user,
                    confirmed_by=request.user,
                )
                messages.success(request, f"{service.name} usage was added to booking #{booking.pk}.")
    return redirect("operations:service_board")


@login_required
@require_POST
def reverse_folio_item(request, item_id):
    _require_billing_staff(request.user)
    reason = request.POST.get("reason", "").strip()[:500]
    if not reason:
        messages.error(request, "Enter a reason to reverse a service charge.")
        return redirect("operations:service_board")
    with transaction.atomic():
        item = get_object_or_404(FolioItem.objects.select_for_update().select_related("booking"), pk=item_id)
        if item.status != FolioItem.Status.COMPLETED:
            messages.error(request, "This service charge has already been reversed.")
        elif hasattr(item.booking, "checkout_invoice") and item.booking.checkout_invoice.status != CheckoutInvoice.Status.DRAFT:
            messages.error(request, "This invoice is finalized; this charge cannot be reversed here.")
        else:
            item.status = FolioItem.Status.REVERSED
            item.reversed_by = request.user
            item.reversed_at = timezone.now()
            item.reversal_reason = reason
            item.save(update_fields=["status", "reversed_by", "reversed_at", "reversal_reason"])
            messages.success(request, "The charge was reversed and retained in the audit history.")
    return redirect("operations:service_board")


@login_required
def final_invoice(request, booking_id):
    _require_billing_staff(request.user)
    booking = get_object_or_404(
        Booking.objects.select_related("customer", "room", "room__room_type"), pk=booking_id
    )
    if booking.status not in {Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT}:
        messages.error(request, "A final invoice is available only for a checked-in stay.")
        return redirect("operations:dashboard")
    invoice, _ = CheckoutInvoice.objects.get_or_create(booking=booking)
    amounts = calculate_invoice_amounts(booking, invoice)
    return render(request, "operations/final_invoice.html", {
        "booking": booking,
        "invoice": invoice,
        "amounts": amounts,
        "service_items": FolioItem.objects.filter(booking=booking).select_related("service"),
        "payments": Payment.objects.filter(booking=booking, status=Payment.Status.SUCCESS).select_related("receipt"),
        "pending_requests": booking.service_requests.filter(status__in=[ServiceRequest.Status.REQUESTED, ServiceRequest.Status.APPROVED]).count(),
        "can_issue": invoice.status == CheckoutInvoice.Status.DRAFT,
        "can_pay": invoice.status in {CheckoutInvoice.Status.ISSUED, CheckoutInvoice.Status.PARTIALLY_PAID},
        "is_staff_view": True,
        "nights": (booking.check_out - booking.check_in).days,
        "room_rate": booking.total_price / max((booking.check_out - booking.check_in).days, 1),
    })


@login_required
@require_POST
def issue_final_invoice(request, booking_id):
    _require_billing_staff(request.user)
    with transaction.atomic():
        booking = get_object_or_404(
            Booking.objects.select_for_update().select_related("room", "room__room_type"), pk=booking_id
        )
        if booking.status != Booking.Status.CHECKED_IN:
            messages.error(request, "Only a checked-in booking can be invoiced for checkout.")
            return redirect("operations:dashboard")
        invoice, _ = CheckoutInvoice.objects.select_for_update().get_or_create(booking=booking)
        if invoice.status != CheckoutInvoice.Status.DRAFT:
            messages.info(request, "This invoice has already been issued.")
            return redirect("operations:invoice", invoice_id=invoice.pk)
        unresolved = booking.service_requests.filter(status__in=[ServiceRequest.Status.REQUESTED, ServiceRequest.Status.APPROVED]).exists()
        if unresolved:
            messages.error(request, "Review all open service requests before issuing the final invoice.")
            return redirect("operations:invoice", invoice_id=invoice.pk)
        amounts = calculate_invoice_amounts(booking, invoice)
        invoice.room_charges = amounts["room_charges"]
        invoice.service_charges = amounts["service_charges"]
        invoice.total_charges = amounts["total_charges"]
        invoice.issued_at = timezone.now()
        invoice.save(update_fields=["room_charges", "service_charges", "total_charges", "issued_at"])
        year = invoice.issued_at.year
        invoice.invoice_number = f"SVHR-INV-{year}-{invoice.pk:06d}"
        invoice.save(update_fields=["invoice_number"])
        InvoiceLine.objects.create(
            invoice=invoice,
            kind=InvoiceLine.Kind.ROOM,
            description=f"{booking.room.room_type.name} · Room {booking.room.room_number} ({(booking.check_out - booking.check_in).days} nights)",
            quantity=(booking.check_out - booking.check_in).days,
            unit_price=booking.total_price / max((booking.check_out - booking.check_in).days, 1),
            line_total=booking.total_price,
        )
        for item in booking.folio_items.filter(status=FolioItem.Status.COMPLETED).select_related("service"):
            InvoiceLine.objects.create(
                invoice=invoice,
                kind=InvoiceLine.Kind.SERVICE,
                description=item.service.name,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
                folio_item=item,
            )
        update_invoice_status(invoice)
        messages.success(request, f"Final invoice {invoice.invoice_number} has been issued.")
    return redirect("operations:invoice", invoice_id=invoice.pk)


def _get_visible_invoice(request, invoice_id):
    invoice = get_object_or_404(
        CheckoutInvoice.objects.select_related("booking", "booking__customer", "booking__room", "booking__room__room_type"),
        pk=invoice_id,
    )
    if _is_billing_staff(request.user):
        return invoice
    if invoice.booking.customer_id != request.user.pk or invoice.status == CheckoutInvoice.Status.DRAFT:
        raise PermissionDenied
    return invoice


@login_required
def invoice_detail(request, invoice_id):
    invoice = _get_visible_invoice(request, invoice_id)
    amounts = calculate_invoice_amounts(invoice.booking, invoice)
    return render(request, "operations/final_invoice.html", {
        "booking": invoice.booking,
        "invoice": invoice,
        "amounts": amounts,
        "service_items": FolioItem.objects.filter(booking=invoice.booking).select_related("service"),
        "payments": Payment.objects.filter(booking=invoice.booking, status=Payment.Status.SUCCESS).select_related("receipt"),
        "can_issue": False,
        "can_pay": invoice.status in {CheckoutInvoice.Status.ISSUED, CheckoutInvoice.Status.PARTIALLY_PAID} and invoice.booking.customer_id == request.user.pk,
        "is_staff_view": _is_billing_staff(request.user),
        "nights": (invoice.booking.check_out - invoice.booking.check_in).days,
        "room_rate": invoice.booking.total_price / max((invoice.booking.check_out - invoice.booking.check_in).days, 1),
    })


@login_required
def invoice_pdf(request, invoice_id):
    invoice = _get_visible_invoice(request, invoice_id)
    from io import BytesIO
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    booking = invoice.booking
    amounts = calculate_invoice_amounts(booking, invoice)
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="InvoiceTitle", parent=styles["Title"], textColor=colors.HexColor("#173b32")))
    story = [
        Paragraph("Serene Valley Hotel &amp; Resort", styles["InvoiceTitle"]),
        Paragraph("FINAL CHECKOUT INVOICE", styles["Heading2"]),
        Paragraph(f"{escape(invoice.invoice_number or 'Draft')} · {invoice.issued_at or invoice.created_at:%d %b %Y %H:%M}", styles["BodyText"]),
        Spacer(1, 6*mm),
        Paragraph(f"Guest: {escape(booking.customer.get_full_name() or booking.customer.get_username())} · Reservation #{booking.pk}", styles["BodyText"]),
        Paragraph(f"Room {escape(booking.room.room_number)} · {escape(booking.room.room_type.name)} · {booking.check_in} to {booking.check_out}", styles["BodyText"]),
        Spacer(1, 5*mm),
    ]
    rows = [["Description", "Qty", "Unit (INR)", "Total (INR)"]]
    for line in invoice.lines.all():
        rows.append([line.description, str(line.quantity), str(line.unit_price), str(line.line_total)])
    rows += [
        ["Room charges", "", "", str(amounts["room_charges"])],
        ["Service charges", "", "", str(amounts["service_charges"])],
        ["Total stay charges", "", "", str(amounts["total_charges"])],
        ["Previously received", "", "", str(amounts["total_paid"])],
        ["Room payment credit", "", "", str(amounts["room_paid"])],
        ["Other invoice payments", "", "", str(amounts["service_paid"])],
        ["Outstanding balance", "", "", str(amounts["outstanding"])],
    ]
    table = Table([[escape(str(cell)) for cell in row] for row in rows], colWidths=[82*mm, 18*mm, 31*mm, 33*mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#173b32")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), .35, colors.HexColor("#d7ded8")),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("PADDING", (0,0), (-1,-1), 7),
    ]))
    story.extend([table, Spacer(1, 8*mm), Paragraph(f"Invoice status: {invoice.get_status_display()} · Currency: INR", styles["BodyText"])])
    doc.build(story)
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice.invoice_number or f"invoice-{invoice.pk}"}.pdf"'
    return response


@login_required
@require_POST
def complete_checkout(request, booking_id):
    _require_billing_staff(request.user)
    with transaction.atomic():
        booking = get_object_or_404(Booking.objects.select_for_update(), pk=booking_id)
        invoice = get_object_or_404(CheckoutInvoice.objects.select_for_update(), booking=booking)
        if booking.status == Booking.Status.CHECKED_OUT:
            messages.info(request, "Guest checkout has already been completed.")
            return redirect("operations:dashboard")
        if booking.status != Booking.Status.CHECKED_IN:
            messages.error(request, "Only a checked-in guest can complete checkout.")
            return redirect("operations:invoice", invoice_id=invoice.pk)
        amounts = calculate_invoice_amounts(booking, invoice)
        if invoice.status != CheckoutInvoice.Status.PAID or amounts["outstanding"] > 0:
            messages.error(request, "Checkout is blocked until the invoice balance is fully settled.")
            return redirect("operations:invoice", invoice_id=invoice.pk)
        booking.transition_to(Booking.Status.CHECKED_OUT)
        messages.success(request, "Guest checkout is complete. The invoice and payment history are retained.")
    return redirect("operations:dashboard")
