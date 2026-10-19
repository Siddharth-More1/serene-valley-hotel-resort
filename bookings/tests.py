from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from properties.models import Room, RoomType
from payments.models import Payment
from .admin import BookingAdmin, BookingAdminForm
from .forms import BookingForm
from .models import Booking


class BookingWorkflowTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username="guest",
            password="StrongPass!9284",
            first_name="Hotel Guest",
        )
        self.other_customer = User.objects.create_user(
            username="otherguest",
            password="StrongPass!9284",
        )
        self.room_type = RoomType.objects.create(
            name="Valley Suite",
            description="A suite overlooking the valley.",
            price_per_night=Decimal("4250.00"),
            capacity=3,
        )
        self.room = Room.objects.create(
            room_number="101",
            room_type=self.room_type,
        )
        self.check_in = timezone.localdate() + timedelta(days=10)
        self.check_out = self.check_in + timedelta(days=3)
        self.create_url = reverse("bookings:create")
        self.client.force_login(self.customer)

    def booking_data(self, **overrides):
        data = {
            "room": self.room.pk,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "guests": 2,
        }
        data.update(overrides)
        return data

    def make_booking(self, *, customer=None, status=Booking.Status.PENDING, **overrides):
        fields = {
            "customer": customer or self.customer,
            "room": self.room,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "guests": 2,
            "total_price": Decimal("12750.00"),
            "status": status,
        }
        fields.update(overrides)
        return Booking.objects.create(**fields)

    def test_booking_form_validates_dates_and_guest_capacity(self):
        same_day = BookingForm(self.booking_data(check_out=self.check_in.isoformat()))
        self.assertFalse(same_day.is_valid())
        self.assertIn("check_out", same_day.errors)

        too_many = BookingForm(self.booking_data(guests=4))
        self.assertFalse(too_many.is_valid())
        self.assertIn("guests", too_many.errors)

        past = BookingForm(self.booking_data(
            check_in=(timezone.localdate() - timedelta(days=1)).isoformat(),
            check_out=timezone.localdate().isoformat(),
        ))
        self.assertFalse(past.is_valid())
        self.assertIn("check_in", past.errors)

    def test_form_rejects_overlapping_active_booking(self):
        self.make_booking(status=Booking.Status.CONFIRMED)
        form = BookingForm(self.booking_data())
        self.assertFalse(form.is_valid())
        self.assertIn("room", form.errors)

    def test_cancelled_and_checked_out_bookings_do_not_block_room(self):
        self.make_booking(status=Booking.Status.CANCELLED)
        self.make_booking(
            status=Booking.Status.CHECKED_OUT,
            check_in=self.check_in + timedelta(days=1),
            check_out=self.check_out + timedelta(days=1),
        )
        form = BookingForm(self.booking_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_guest_must_be_authenticated_to_create_booking(self):
        self.client.logout()
        response = self.client.post(self.create_url, self.booking_data())
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={self.create_url}",
            fetch_redirect_response=False,
        )
        self.assertEqual(Booking.objects.count(), 0)

    def test_booking_uses_server_price_and_requested_number_of_nights(self):
        response = self.client.post(
            self.create_url,
            self.booking_data(total_price="0.01", customer=self.other_customer.pk),
        )
        booking = Booking.objects.get()
        self.assertRedirects(response, reverse("bookings:my_bookings"), fetch_redirect_response=False)
        self.assertEqual(booking.customer, self.customer)
        self.assertEqual(booking.total_price, Decimal("12750.00"))
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_final_availability_check_prevents_duplicate_booking(self):
        self.make_booking()
        response = self.client.post(self.create_url, self.booking_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already booked for those dates")
        self.assertEqual(Booking.objects.count(), 1)

    def test_room_type_card_shows_availability_and_book_now_link(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Valley Suite")
        self.assertContains(response, "available today")
        self.assertContains(response, f"{self.create_url}?room_type={self.room_type.pk}")
        self.assertContains(response, "Book Now")

        self.make_booking(
            status=Booking.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
        )
        response = self.client.get(reverse("home"))
        self.assertContains(response, "No rooms available today")

    def test_room_type_query_filters_room_choices(self):
        other_type = RoomType.objects.create(
            name="Garden Room",
            description="Garden view.",
            price_per_night=Decimal("1000.00"),
            capacity=2,
        )
        other_room = Room.objects.create(room_number="202", room_type=other_type)
        response = self.client.get(f"{self.create_url}?room_type={self.room_type.pk}")
        self.assertContains(response, "101 - Valley Suite")
        self.assertNotContains(response, "202 - Garden Room")

    def test_customers_only_see_their_own_bookings_and_details(self):
        own = self.make_booking()
        other = self.make_booking(customer=self.other_customer, room=Room.objects.create(
            room_number="102", room_type=self.room_type,
        ))
        response = self.client.get(reverse("bookings:my_bookings"))
        self.assertEqual(
            list(response.context["bookings"].values_list("pk", flat=True)),
            [own.pk],
        )
        self.assertEqual(
            self.client.get(reverse("bookings:detail", args=[other.pk])).status_code,
            404,
        )

    def test_customer_can_cancel_only_pending_booking(self):
        pending = self.make_booking()
        response = self.client.post(reverse("bookings:cancel", args=[pending.pk]))
        pending.refresh_from_db()
        self.assertEqual(pending.status, Booking.Status.CANCELLED)
        self.assertRedirects(response, reverse("bookings:my_bookings"), fetch_redirect_response=False)

        confirmed = self.make_booking(
            status=Booking.Status.CONFIRMED,
            room=Room.objects.create(room_number="303", room_type=self.room_type),
        )
        response = self.client.post(reverse("bookings:cancel", args=[confirmed.pk]))
        confirmed.refresh_from_db()
        self.assertEqual(confirmed.status, Booking.Status.CONFIRMED)
        self.assertRedirects(response, reverse("bookings:my_bookings"), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("bookings:my_bookings")), "Only pending bookings")

    def test_cancellation_requires_post_and_csrf_token(self):
        booking = self.make_booking()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.customer)
        url = reverse("bookings:cancel", args=[booking.pk])

        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url).status_code, 403)
        client.get(reverse("bookings:my_bookings"))
        token = client.cookies["csrftoken"].value
        response = client.post(url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

    def test_staff_roles_are_required_for_booking_admin(self):
        model_admin = BookingAdmin(Booking, admin.site)
        request = type("Request", (), {})()
        request.user = self.customer
        self.assertFalse(model_admin.has_module_permission(request))

        receptionist = User.objects.create_user(
            username="desk",
            password="StrongPass!9284",
            role=User.Role.RECEPTIONIST,
            is_staff=True,
        )
        request.user = receptionist
        self.assertTrue(model_admin.has_module_permission(request))
        self.assertTrue(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_booking_admin_allows_only_valid_status_transitions(self):
        booking = self.make_booking()
        model_admin = BookingAdmin(Booking, admin.site)
        manager = User.objects.create_user(
            username="manager",
            password="StrongPass!9284",
            role=User.Role.MANAGER,
            is_staff=True,
        )
        request = type("Request", (), {"user": manager})()

        invalid_form = BookingAdminForm(instance=booking, data={"status": Booking.Status.CHECKED_IN})
        self.assertFalse(invalid_form.is_valid())
        self.assertIn("status", invalid_form.errors)

        form = BookingAdminForm(instance=booking, data={"status": Booking.Status.CONFIRMED})
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.save(commit=False), form, change=True)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

        form = BookingAdminForm(instance=booking, data={"status": Booking.Status.CHECKED_IN})
        self.assertFalse(form.is_valid())
        self.assertIn("verified successful payment", str(form.errors))

        Payment.objects.create(
            booking=booking,
            amount=booking.total_price,
            status=Payment.Status.SUCCESS,
        )
        form = BookingAdminForm(instance=booking, data={"status": Booking.Status.CHECKED_IN})
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.save(commit=False), form, change=True)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)

        with self.assertRaises(ValidationError):
            booking.transition_to(Booking.Status.PENDING)

    def test_manager_cannot_confirm_overlapping_booking_or_cancel_unresolved_payment(self):
        booking = self.make_booking()
        self.make_booking()
        form = BookingAdminForm(instance=booking, data={"status": Booking.Status.CONFIRMED})
        self.assertFalse(form.is_valid())
        self.assertIn("overlapping dates", str(form.errors))

        second_room = Room.objects.create(room_number="405", room_type=self.room_type)
        paid_booking = self.make_booking(room=second_room)
        Payment.objects.create(
            booking=paid_booking,
            amount=paid_booking.total_price,
            status=Payment.Status.SUCCESS,
        )
        form = BookingAdminForm(instance=paid_booking, data={"status": Booking.Status.CANCELLED})
        self.assertFalse(form.is_valid())
        self.assertIn("successful payment", str(form.errors))
