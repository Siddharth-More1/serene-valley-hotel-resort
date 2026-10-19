from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.test import RequestFactory, TestCase
from django.utils import timezone

from accounts.models import User
from bookings.models import Booking
from .admin import RoomAdmin, RoomAdminForm, RoomTypeAdmin, RoomTypeAdminForm, ServiceAdmin
from .models import Room, RoomType, Service


class PropertyAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.manager = User.objects.create_user(
            username="manager",
            password="StrongPass!9284",
            role=User.Role.MANAGER,
            is_staff=True,
        )
        self.receptionist = User.objects.create_user(
            username="receptionist",
            password="StrongPass!9284",
            role=User.Role.RECEPTIONIST,
            is_staff=True,
        )
        self.customer = User.objects.create_user(
            username="guest",
            password="StrongPass!9284",
        )

    def test_room_type_room_and_service_admin_are_manager_only(self):
        registrations = (
            (RoomType, RoomTypeAdmin),
            (Room, RoomAdmin),
            (Service, ServiceAdmin),
        )
        for model, admin_class in registrations:
            model_admin = admin_class(model, admin.site)
            request = self.factory.get("/admin/")
            request.user = self.manager
            self.assertTrue(model_admin.has_module_permission(request))
            self.assertTrue(model_admin.has_add_permission(request))

            request.user = self.receptionist
            self.assertFalse(model_admin.has_module_permission(request))
            request.user = self.customer
            self.assertFalse(model_admin.has_module_permission(request))

    def test_room_type_admin_exposes_image_upload_and_preview(self):
        model_admin = RoomTypeAdmin(RoomType, admin.site)
        self.assertIn("image", model_admin.fields)
        self.assertIn("image_preview", model_admin.readonly_fields)
        room_type = RoomType(name="Suite", description="Description", price_per_night="100.00")
        self.assertEqual(model_admin.image_preview(room_type), "No image uploaded")

    def test_room_type_capacity_cannot_be_reduced_below_active_booking_guests(self):
        room_type = RoomType.objects.create(
            name="Suite",
            description="Description",
            price_per_night=Decimal("100.00"),
            capacity=4,
        )
        room = Room.objects.create(room_number="301", room_type=room_type)
        Booking.objects.create(
            customer=self.customer,
            room=room,
            check_in=timezone.localdate() + timedelta(days=3),
            check_out=timezone.localdate() + timedelta(days=5),
            guests=3,
            total_price=Decimal("200.00"),
        )
        form = RoomTypeAdminForm(instance=room_type, data={
            "name": "Suite",
            "description": "Description",
            "price_per_night": "100.00",
            "capacity": 2,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("capacity", form.errors)

    def test_room_type_and_maintenance_changes_are_blocked_during_active_stays(self):
        room_type = RoomType.objects.create(
            name="Suite",
            description="Description",
            price_per_night=Decimal("100.00"),
        )
        another_type = RoomType.objects.create(
            name="Cabin",
            description="Description",
            price_per_night=Decimal("80.00"),
        )
        room = Room.objects.create(room_number="302", room_type=room_type)
        Booking.objects.create(
            customer=self.customer,
            room=room,
            check_in=timezone.localdate() + timedelta(days=3),
            check_out=timezone.localdate() + timedelta(days=5),
            guests=1,
            total_price=Decimal("200.00"),
        )

        form = RoomAdminForm(instance=room, data={
            "room_number": "302",
            "room_type": another_type.pk,
            "status": Room.Status.AVAILABLE,
            "floor": room.floor,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("room_type", form.errors)

        form = RoomAdminForm(instance=room, data={
            "room_number": "302",
            "room_type": room_type.pk,
            "status": Room.Status.MAINTENANCE,
            "floor": room.floor,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)
