
from datetime import date

from django import forms
from django.utils import timezone

from .availability import available_rooms_for_stay, room_has_conflict
from properties.models import Room
from .models import Booking


class BookingForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = ["room", "check_in", "check_out", "guests"]

        widgets = {
            "check_in": forms.DateInput(attrs={"type": "date"}),
            "check_out": forms.DateInput(attrs={"type": "date"}),
            "guests": forms.NumberInput(attrs={"min": 1}),
        }

    def __init__(self, *args, room_type_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        rooms = Room.objects.filter(status=Room.Status.AVAILABLE)
        room_type_filter = None
        if room_type_id:
            try:
                room_type_id = int(room_type_id)
            except (TypeError, ValueError):
                rooms = rooms.none()
                room_type_filter = -1
            else:
                room_type_filter = room_type_id
                rooms = rooms.filter(room_type_id=room_type_id)
        if not self.is_bound:
            check_in = self.initial.get("check_in")
            check_out = self.initial.get("check_out")
            try:
                check_in = check_in if isinstance(check_in, date) else date.fromisoformat(check_in or "")
                check_out = check_out if isinstance(check_out, date) else date.fromisoformat(check_out or "")
            except (TypeError, ValueError):
                pass
            else:
                if check_out > check_in:
                    rooms = available_rooms_for_stay(
                        check_in, check_out, room_type_id=room_type_filter
                    )
        self.fields["room"].queryset = rooms.select_related(
            "room_type"
        ).order_by("room_number")
        self.fields["check_in"].widget.attrs["min"] = timezone.localdate().isoformat()
        self.fields["check_out"].widget.attrs["min"] = timezone.localdate().isoformat()

    def clean(self):
        cleaned_data = super().clean()

        check_in = cleaned_data.get("check_in")
        check_out = cleaned_data.get("check_out")
        guests = cleaned_data.get("guests")
        room = cleaned_data.get("room")

        if check_in and check_in < timezone.localdate():
            self.add_error("check_in", "Check-in cannot be in the past.")

        if check_in and check_out and check_out <= check_in:
            self.add_error(
                "check_out",
                "Check-out must be after check-in."
            )

        if guests and guests < 1:
            self.add_error("guests", "At least one guest is required.")

        if room and guests and guests > room.room_type.capacity:
            self.add_error(
                "guests",
                f"This room supports up to {room.room_type.capacity} guests."
            )

        if room and check_in and check_out and check_out > check_in:
            if room_has_conflict(room.pk, check_in, check_out):
                self.add_error("room", "This room is already booked for those dates. Choose another room or dates.")

        return cleaned_data
