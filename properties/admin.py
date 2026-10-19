from django.contrib import admin
from django import forms
from django.core.exceptions import ValidationError
from django.utils.html import format_html

from accounts.models import User
from bookings.models import Booking
from .models import Room, RoomType, Service


ACTIVE_BOOKING_STATUSES = (
    Booking.Status.PENDING,
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
)


class ManagerOnlyAdmin(admin.ModelAdmin):
    def can_manage(self, request):
        return request.user.is_superuser or (
            request.user.is_staff and request.user.role == User.Role.MANAGER
        )

    def has_module_permission(self, request):
        return self.can_manage(request)

    def has_view_permission(self, request, obj=None):
        return self.can_manage(request)

    def has_change_permission(self, request, obj=None):
        return self.can_manage(request)

    def has_add_permission(self, request):
        return self.can_manage(request)

    def has_delete_permission(self, request, obj=None):
        return self.can_manage(request)


class RoomTypeAdminForm(forms.ModelForm):
    class Meta:
        model = RoomType
        fields = ("name", "description", "price_per_night", "capacity", "image")

    def clean_capacity(self):
        capacity = self.cleaned_data["capacity"]
        if self.instance.pk:
            room_type = RoomType.objects.select_for_update().get(pk=self.instance.pk)
        else:
            room_type = self.instance
        if self.instance.pk and Booking.objects.filter(
            room__room_type=room_type,
            status__in=ACTIVE_BOOKING_STATUSES,
            guests__gt=capacity,
        ).exists():
            raise forms.ValidationError(
                "Capacity cannot be reduced below the guest count on an active booking."
            )
        return capacity


class RoomAdminForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ("room_number", "room_type", "status", "floor")

    def clean(self):
        cleaned_data = super().clean()
        if not self.instance.pk:
            return cleaned_data
        previous = Room.objects.select_for_update().get(pk=self.instance.pk)
        has_active_bookings = Booking.objects.filter(
            room=previous,
            status__in=ACTIVE_BOOKING_STATUSES,
        ).exists()
        if has_active_bookings:
            if cleaned_data.get("room_type") != previous.room_type:
                self.add_error("room_type", "Room type cannot change while this room has active bookings.")
            if (
                cleaned_data.get("status") == Room.Status.MAINTENANCE
                and previous.status != Room.Status.MAINTENANCE
            ):
                self.add_error("status", "Resolve active bookings before placing this room under maintenance.")
        return cleaned_data


@admin.register(RoomType)
class RoomTypeAdmin(ManagerOnlyAdmin):
    form = RoomTypeAdminForm
    list_display = ("name", "price_per_night", "capacity", "room_count", "image_preview")
    search_fields = ("name", "description")
    list_filter = ("capacity",)
    fields = ("name", "description", "price_per_night", "capacity", "image", "image_preview")
    readonly_fields = ("image_preview",)

    @admin.display(description="Rooms")
    def room_count(self, obj):
        return obj.rooms.count()

    @admin.display(description="Current image")
    def image_preview(self, obj):
        if not obj or not obj.image:
            return "No image uploaded"
        return format_html('<img src="{}" alt="{}" style="max-height: 90px; max-width: 160px;">', obj.image.url, obj.name)


@admin.register(Room)
class RoomAdmin(ManagerOnlyAdmin):
    form = RoomAdminForm
    list_display = ("room_number", "room_type", "floor", "status")
    list_filter = ("status", "room_type", "floor")
    search_fields = ("room_number", "room_type__name")
    autocomplete_fields = ("room_type",)


@admin.register(Service)
class ServiceAdmin(ManagerOnlyAdmin):
    list_display = ("name", "price", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
