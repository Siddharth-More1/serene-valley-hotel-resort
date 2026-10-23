
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.views.decorators.http import require_POST
from django.shortcuts import render, redirect
from django.contrib import messages

from properties.models import Room
from .availability import lock_room_for_inventory, room_has_conflict
from .forms import BookingForm
from .models import Booking
from payments.models import Payment
from operations.models import CheckoutInvoice


@login_required
def create_booking(request):
    room_type_id = request.GET.get("room_type")
    if request.method == "POST":
        form = BookingForm(request.POST, room_type_id=room_type_id)

        if form.is_valid():
            room = form.cleaned_data["room"]
            check_in = form.cleaned_data["check_in"]
            check_out = form.cleaned_data["check_out"]

            with transaction.atomic():
                # Serialize inventory writes before the final overlap check.
                room = lock_room_for_inventory(room.pk)
                overlapping = room_has_conflict(room.pk, check_in, check_out)

                if form.cleaned_data["guests"] > room.room_type.capacity:
                    form.add_error(
                        "guests",
                        f"This room supports up to {room.room_type.capacity} guests.",
                    )
                elif room.status != Room.Status.AVAILABLE:
                    form.add_error("room", "This room is under maintenance.")
                elif overlapping:
                    form.add_error("room", "This room is already booked for those dates.")
                else:
                    booking = form.save(commit=False)
                    booking.room = room
                    booking.customer = request.user
                    booking.total_price = (
                        room.room_type.price_per_night * (check_out - check_in).days
                    )
                    booking.save()

            if not form.errors:
                messages.success(
                    request,
                    "Your booking request has been submitted successfully."
                )
                return redirect("bookings:my_bookings")
    else:
        form = BookingForm(
            room_type_id=room_type_id,
            initial={
                field: request.GET[field]
                for field in ("check_in", "check_out")
                if request.GET.get(field)
            },
        )

    return render(request, "bookings/create_booking.html", {"form": form})


@login_required
def my_bookings(request):
    bookings = Booking.objects.filter(
        customer=request.user
    ).select_related("room", "room__room_type").order_by("-created_at")

    return render(
        request,
        "bookings/my_bookings.html",
        {"bookings": bookings},
    )

@login_required
def booking_detail(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.select_related("room", "room__room_type"),
        id=booking_id,
        customer=request.user,
    )

    return render(
        request,
        "bookings/booking_detail.html",
        {
            "booking": booking,
            "has_successful_payment": Payment.objects.filter(
                booking=booking, status=Payment.Status.SUCCESS
            ).exists(),
            "service_requests": booking.service_requests.select_related("service").all(),
            "checkout_invoice": CheckoutInvoice.objects.filter(booking=booking).first(),
        },
    )

@login_required
@require_POST
def cancel_booking(request, booking_id):
    with transaction.atomic():
        booking = get_object_or_404(
            Booking.objects.select_for_update(),
            id=booking_id,
            customer=request.user,
        )

        if booking.status == Booking.Status.PENDING:
            booking.status = Booking.Status.CANCELLED
            booking.save(update_fields=["status"])
            messages.success(request, "Booking cancelled successfully.")
        else:
            messages.error(
                request,
                "Only pending bookings can be cancelled online."
            )

    return redirect("bookings:my_bookings")
