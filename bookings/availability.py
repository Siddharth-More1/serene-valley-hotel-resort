from django.db import connection
from django.db.models import Exists, F, OuterRef, Q

from properties.models import Room

from .models import Booking


DATE_HOLDING_STATUSES = (Booking.Status.PENDING, Booking.Status.CONFIRMED)


def _conflicting_bookings(check_in, check_out):
    if check_out <= check_in:
        raise ValueError("Check-out must be after check-in.")

    return Booking.objects.filter(
        Q(status=Booking.Status.CHECKED_IN)
        | Q(
            status__in=DATE_HOLDING_STATUSES,
            check_in__lt=check_out,
            check_out__gt=check_in,
        )
    )


def room_has_conflict(room_id, check_in, check_out, *, exclude_booking_id=None):
    conflicts = _conflicting_bookings(check_in, check_out).filter(room_id=room_id)
    if exclude_booking_id is not None:
        conflicts = conflicts.exclude(pk=exclude_booking_id)
    return conflicts.exists()


def available_rooms_for_stay(check_in, check_out, *, room_type_id=None):
    conflicts = _conflicting_bookings(check_in, check_out).filter(room_id=OuterRef("pk"))
    rooms = Room.objects.filter(status=Room.Status.AVAILABLE).annotate(
        has_booking_conflict=Exists(conflicts),
    ).filter(has_booking_conflict=False)
    if room_type_id is not None:
        rooms = rooms.filter(room_type_id=room_type_id)
    return rooms


def lock_room_for_inventory(room_id):
    """Serialize reservation writes for one room on the configured database."""
    if not connection.in_atomic_block:
        raise RuntimeError("Room inventory must be locked inside a transaction.")

    if connection.features.has_select_for_update:
        return Room.objects.select_for_update().select_related("room_type").get(pk=room_id)

    if connection.vendor == "sqlite":
        # SQLite ignores SELECT FOR UPDATE. Acquire its database write lock before
        # reading bookings so concurrent reservation writers recheck after commit.
        Room.objects.filter(pk=room_id).update(status=F("status"))

    return Room.objects.select_related("room_type").get(pk=room_id)
