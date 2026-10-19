from datetime import timedelta

from django.db.models import Exists, OuterRef
from django.shortcuts import render
from django.utils import timezone
from properties.models import Room, RoomType, Service
from bookings.models import Booking


def home(request):
    today = timezone.localdate()
    rooms = RoomType.objects.all().order_by("name")
    for room_type in rooms:
        active_stays = Booking.objects.filter(
            room_id=OuterRef("pk"),
            check_in__lt=today + timedelta(days=1),
            check_out__gt=today,
        ).exclude(status__in=[Booking.Status.CANCELLED, Booking.Status.CHECKED_OUT])
        room_type.available_room_count = Room.objects.filter(
            room_type=room_type,
            status=Room.Status.AVAILABLE,
        ).annotate(
            has_active_stay=Exists(active_stays),
        ).filter(has_active_stay=False).count()
    services = Service.objects.filter(is_active=True)[:6]

    return render(
        request,
        "home.html",
        {
            "rooms": rooms,
            "services": services,
        },
    )
