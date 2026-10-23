from datetime import timedelta

from django.shortcuts import render
from django.utils import timezone
from properties.models import RoomType, Service
from bookings.availability import available_rooms_for_stay


def home(request):
    today = timezone.localdate()
    rooms = RoomType.objects.all().order_by("name")
    for room_type in rooms:
        room_type.available_room_count = available_rooms_for_stay(
            today, today + timedelta(days=1), room_type_id=room_type.pk
        ).count()
    services = Service.objects.filter(is_active=True)[:6]

    return render(
        request,
        "home.html",
        {
            "rooms": rooms,
            "services": services,
        },
    )
