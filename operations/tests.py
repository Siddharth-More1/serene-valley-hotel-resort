from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import User
from bookings.models import Booking
from payments.models import Payment
from properties.models import Room, RoomType, Service
from .billing import calculate_invoice_amounts, update_invoice_status
from .models import CheckoutInvoice, FolioItem, InvoiceLine, ServiceRequest


class ManagerDashboardTests(TestCase):
    def setUp(self):
        self.url = reverse("operations:dashboard")
        self.manager = User.objects.create_user(
            username="manager",
            password="StrongPass!9284",
            role=User.Role.MANAGER,
            is_staff=True,
        )
        self.customer = User.objects.create_user(
            username="guest",
            password="StrongPass!9284",
        )
        self.room_type = RoomType.objects.create(
            name="Garden Room",
            description="Garden view.",
            price_per_night=Decimal("2000.00"),
        )
        self.room = Room.objects.create(room_number="101", room_type=self.room_type)

    def make_booking(self, *, status, room=None, customer=None):
        today = timezone.localdate()
        return Booking.objects.create(
            customer=customer or self.customer,
            room=room or self.room,
            check_in=today,
            check_out=today + timedelta(days=1),
            guests=1,
            total_price=Decimal("2000.00"),
            status=status,
        )

    def test_anonymous_users_are_redirected_and_customers_are_forbidden(self):
        response = self.client.get(self.url)
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={self.url}",
            fetch_redirect_response=False,
        )
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_dashboard_counts_bookings_occupancy_and_verified_revenue(self):
        self.make_booking(status=Booking.Status.CHECKED_IN)
        pending_booking = self.make_booking(
            status=Booking.Status.PENDING,
            room=Room.objects.create(room_number="102", room_type=self.room_type),
        )
        successful_booking = self.make_booking(
            status=Booking.Status.CONFIRMED,
            room=Room.objects.create(room_number="103", room_type=self.room_type),
        )
        Payment.objects.create(
            booking=successful_booking,
            amount=Decimal("1500.00"),
            status=Payment.Status.SUCCESS,
        )
        Payment.objects.create(
            booking=pending_booking,
            amount=Decimal("2000.00"),
            status=Payment.Status.PENDING,
        )
        Payment.objects.create(
            booking=pending_booking,
            amount=Decimal("500.00"),
            status=Payment.Status.FAILED,
        )
        Room.objects.create(
            room_number="104",
            room_type=self.room_type,
            status=Room.Status.MAINTENANCE,
        )

        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_bookings"], 3)
        self.assertEqual(response.context["pending_bookings"], 1)
        self.assertEqual(response.context["occupied_rooms"], 1)
        self.assertEqual(response.context["active_rooms"], 3)
        self.assertEqual(response.context["occupancy_percent"], 33.3)
        self.assertEqual(response.context["verified_revenue"], Decimal("1500.00"))
        self.assertNotContains(response, "provider_payment_id")

    def test_empty_inventory_has_helpful_occupancy_message(self):
        self.room.delete()
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertIsNone(response.context["occupancy_percent"])
        self.assertContains(response, "Occupancy cannot be calculated")
        self.assertContains(response, "There are no bookings yet")


class ServiceBillingWorkflowTests(TestCase):
    def setUp(self):
        today = timezone.localdate()
        self.guest = User.objects.create_user(
            username="folio_guest", password="StrongPass!9284", first_name="Valley Guest", email="guest@example.test", phone="555-0101"
        )
        self.other_guest = User.objects.create_user(username="other_folio_guest", password="StrongPass!9284")
        self.manager = User.objects.create_user(
            username="folio_manager", password="StrongPass!9284", role=User.Role.MANAGER, is_staff=True
        )
        self.receptionist = User.objects.create_user(
            username="folio_desk", password="StrongPass!9284", role=User.Role.RECEPTIONIST, is_staff=True
        )
        self.room_type = RoomType.objects.create(name="Creek Suite", description="Quiet suite", price_per_night=Decimal("500.00"), capacity=2)
        self.room = Room.objects.create(room_number="F-101", room_type=self.room_type)
        self.booking = Booking.objects.create(
            customer=self.guest, room=self.room, check_in=today,
            check_out=today + timedelta(days=2), guests=2,
            total_price=Decimal("1000.00"), status=Booking.Status.CHECKED_IN,
        )
        self.service = Service.objects.create(name="Breakfast", description="Breakfast service", price=Decimal("25.00"))
        self.room_payment = Payment.objects.create(
            booking=self.booking, kind=Payment.Kind.ROOM,
            amount=Decimal("900.00"), status=Payment.Status.SUCCESS,
            provider_order_id="room_order", provider_payment_id="room_payment",
        )

    def add_usage(self, *, qty=1, unit_price=Decimal("25.00"), status=FolioItem.Status.COMPLETED):
        return FolioItem.objects.create(
            booking=self.booking, service=self.service, quantity=qty,
            unit_price=unit_price, line_total=unit_price * qty,
            created_by=self.manager, confirmed_by=self.manager, status=status,
        )

    def issue_invoice(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("operations:final_invoice", args=[self.booking.pk]))
        invoice = CheckoutInvoice.objects.get(booking=self.booking)
        if invoice.status == CheckoutInvoice.Status.DRAFT:
            response = self.client.post(reverse("operations:issue_final_invoice", args=[self.booking.pk]))
            invoice.refresh_from_db()
        return invoice, response

    def test_customer_can_request_own_service_and_price_is_snapshotted(self):
        self.client.force_login(self.guest)
        url = reverse("operations:service_request", args=[self.booking.pk])
        self.assertContains(self.client.get(url), "INR 25.00 per unit")
        response = self.client.post(url, {"service": self.service.pk, "quantity": 2, "customer_note": "No nuts"})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        service_request = ServiceRequest.objects.get(booking=self.booking)
        self.assertEqual(service_request.price_at_request, Decimal("25.00"))
        self.assertEqual(service_request.status, ServiceRequest.Status.REQUESTED)
        self.service.price = Decimal("40.00")
        self.service.save(update_fields=["price"])

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("operations:service_request_update", args=[service_request.pk]),
            {"action": "complete", "staff_note": "Delivered"},
        )
        self.assertEqual(response.status_code, 302)
        service_request.refresh_from_db()
        usage = FolioItem.objects.get(request=service_request)
        self.assertEqual(service_request.status, ServiceRequest.Status.COMPLETED)
        self.assertEqual(usage.unit_price, Decimal("25.00"))
        self.assertEqual(usage.line_total, Decimal("50.00"))
        self.client.post(
            reverse("operations:service_request_update", args=[service_request.pk]),
            {"action": "complete"},
        )
        self.assertEqual(FolioItem.objects.filter(request=service_request).count(), 1)

    def test_early_checked_in_stay_allows_customer_requests_and_staff_usage(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.booking.check_in = tomorrow
        self.booking.check_out = tomorrow + timedelta(days=2)
        self.booking.save(update_fields=["check_in", "check_out"])
        customer_url = reverse("operations:service_request", args=[self.booking.pk])

        self.client.force_login(self.guest)
        response = self.client.get(customer_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Breakfast")
        self.assertContains(response, "INR 25.00 per unit")
        response = self.client.post(customer_url, {"service": self.service.pk, "quantity": 1})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ServiceRequest.objects.filter(booking=self.booking).exists())

        self.client.force_login(self.receptionist)
        staff_url = reverse("operations:add_folio_item", args=[self.booking.pk])
        response = self.client.post(staff_url, {"service": self.service.pk, "quantity": 2})
        self.assertEqual(response.status_code, 302)
        item = FolioItem.objects.get(booking=self.booking, request__isnull=True)
        self.assertEqual(item.line_total, Decimal("50.00"))

    def test_unchecked_in_booking_cannot_request_or_record_services(self):
        self.booking.status = Booking.Status.CONFIRMED
        self.booking.save(update_fields=["status"])
        customer_url = reverse("operations:service_request", args=[self.booking.pk])

        self.client.force_login(self.guest)
        response = self.client.get(customer_url)
        self.assertFalse(response.context["can_request"])
        self.assertContains(response, "Service requests are available only during your checked-in stay.")
        self.client.post(customer_url, {"service": self.service.pk, "quantity": 1})
        self.assertFalse(ServiceRequest.objects.filter(booking=self.booking).exists())

        self.client.force_login(self.receptionist)
        staff_url = reverse("operations:add_folio_item", args=[self.booking.pk])
        self.client.post(staff_url, {"service": self.service.pk, "quantity": 1})
        self.assertFalse(FolioItem.objects.filter(booking=self.booking).exists())

    def test_inactive_services_are_hidden_and_cannot_be_requested(self):
        inactive = Service.objects.create(
            name="Inactive Spa", description="Temporarily unavailable", price=Decimal("90.00"), is_active=False
        )
        self.client.force_login(self.guest)
        url = reverse("operations:service_request", args=[self.booking.pk])
        response = self.client.get(url)
        self.assertContains(response, "Breakfast")
        self.assertNotContains(response, "Inactive Spa")
        self.assertFalse(response.context["form"].fields["service"].queryset.filter(pk=inactive.pk).exists())
        self.client.post(url, {"service": inactive.pk, "quantity": 1})
        self.assertFalse(ServiceRequest.objects.filter(booking=self.booking).exists())

    def test_customer_cannot_request_another_guest_booking_or_exceed_quantity(self):
        other_booking = Booking.objects.create(
            customer=self.other_guest, room=Room.objects.create(room_number="F-102", room_type=self.room_type),
            check_in=self.booking.check_in, check_out=self.booking.check_out, guests=1,
            total_price=Decimal("1000.00"), status=Booking.Status.CHECKED_IN,
        )
        self.client.force_login(self.guest)
        response = self.client.post(
            reverse("operations:service_request", args=[other_booking.pk]),
            {"service": self.service.pk, "quantity": 1},
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            reverse("operations:service_request", args=[self.booking.pk]),
            {"service": self.service.pk, "quantity": 101},
        )
        self.assertEqual(ServiceRequest.objects.count(), 0)
        self.assertEqual(response.status_code, 200)

    def test_only_authorized_staff_can_record_usage_and_price_comes_from_database(self):
        url = reverse("operations:add_folio_item", args=[self.booking.pk])
        self.client.force_login(self.guest)
        self.assertEqual(self.client.post(url, {"service": self.service.pk, "quantity": 1}).status_code, 403)
        self.client.force_login(self.receptionist)
        response = self.client.post(url, {"service": self.service.pk, "quantity": 3, "note": "Provided"})
        self.assertEqual(response.status_code, 302)
        item = FolioItem.objects.get(booking=self.booking)
        self.assertEqual(item.unit_price, Decimal("25.00"))
        self.assertEqual(item.line_total, Decimal("75.00"))
        self.assertEqual(item.created_by, self.receptionist)
        self.assertEqual(item.confirmed_by, self.receptionist)

    def test_unfulfilled_and_reversed_services_are_not_billed_and_corrections_are_audited(self):
        self.add_usage(qty=2, unit_price=Decimal("10.00"), status=FolioItem.Status.REVERSED)
        self.assertEqual(calculate_invoice_amounts(self.booking)["service_charges"], Decimal("0.00"))
        pending = ServiceRequest.objects.create(
            booking=self.booking, service=self.service, quantity=2, price_at_request=Decimal("25.00")
        )
        self.assertEqual(calculate_invoice_amounts(self.booking)["service_charges"], Decimal("0.00"))
        item = self.add_usage(qty=2)
        self.client.force_login(self.manager)
        response = self.client.post(reverse("operations:reverse_folio_item", args=[item.pk]), {"reason": "Entered in error"})
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.status, FolioItem.Status.REVERSED)
        self.assertEqual(item.reversed_by, self.manager)
        self.assertEqual(item.reversal_reason, "Entered in error")
        self.assertEqual(calculate_invoice_amounts(self.booking)["service_charges"], Decimal("0.00"))

    def test_invoice_charges_verified_credits_and_partial_payments(self):
        self.add_usage(qty=2)
        invoice, response = self.issue_invoice()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(invoice.room_charges, Decimal("1000.00"))
        self.assertEqual(invoice.service_charges, Decimal("50.00"))
        self.assertEqual(invoice.total_charges, Decimal("1050.00"))
        self.assertEqual(invoice.status, CheckoutInvoice.Status.PARTIALLY_PAID)
        self.assertEqual(invoice.lines.count(), 2)
        amounts = calculate_invoice_amounts(self.booking, invoice)
        self.assertEqual(amounts["room_paid"], Decimal("900.00"))
        self.assertEqual(amounts["outstanding"], Decimal("150.00"))
        # Pending, failed, and refunded records are not retained funds.
        Payment.objects.create(booking=self.booking, invoice=invoice, kind=Payment.Kind.SERVICE, amount=Decimal("100.00"), status=Payment.Status.PENDING)
        Payment.objects.filter(booking=self.booking, invoice=invoice, kind=Payment.Kind.SERVICE, status=Payment.Status.PENDING).update(status=Payment.Status.FAILED)
        Payment.objects.create(booking=self.booking, invoice=invoice, kind=Payment.Kind.SERVICE, amount=Decimal("100.00"), status=Payment.Status.REFUNDED)
        self.assertEqual(calculate_invoice_amounts(self.booking, invoice)["outstanding"], Decimal("150.00"))

        Payment.objects.create(
            booking=self.booking, invoice=invoice, kind=Payment.Kind.SERVICE,
            amount=Decimal("60.00"), status=Payment.Status.SUCCESS,
            provider_order_id="part_order", provider_payment_id="part_payment",
        )
        Payment.objects.create(
            booking=self.booking, invoice=invoice, kind=Payment.Kind.SERVICE,
            amount=Decimal("40.00"), status=Payment.Status.PENDING,
        )
        amounts = calculate_invoice_amounts(self.booking, invoice)
        self.assertEqual(amounts["service_paid"], Decimal("60.00"))
        self.assertEqual(amounts["outstanding"], Decimal("90.00"))
        update_invoice_status(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, CheckoutInvoice.Status.PARTIALLY_PAID)

    def test_invoice_is_idempotent_immutable_and_customer_owned(self):
        self.add_usage()
        invoice, _ = self.issue_invoice()
        self.client.force_login(self.manager)
        self.client.post(reverse("operations:issue_final_invoice", args=[self.booking.pk]))
        invoice.refresh_from_db()
        self.assertEqual(invoice.lines.count(), 2)
        self.assertTrue(invoice.invoice_number.startswith("SVHR-INV-"))
        with self.assertRaises(ValueError):
            invoice.lines.first().delete()
        self.client.force_login(self.guest)
        self.assertEqual(self.client.get(reverse("operations:invoice", args=[invoice.pk])).status_code, 200)
        invoice_pdf = self.client.get(reverse("operations:invoice_pdf", args=[invoice.pk]))
        self.assertEqual(invoice_pdf.status_code, 200)
        self.assertEqual(invoice_pdf["Content-Type"], "application/pdf")
        self.assertTrue(invoice_pdf.content.startswith(b"%PDF-"))
        self.client.force_login(self.other_guest)
        self.assertEqual(self.client.get(reverse("operations:invoice", args=[invoice.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("operations:invoice_pdf", args=[invoice.pk])).status_code, 403)

    def test_unpaid_balance_blocks_checkout_until_verified_funds_cover_total(self):
        self.room_payment.amount = Decimal("1000.00")
        self.room_payment.save(update_fields=["amount"])
        self.add_usage(qty=2, unit_price=Decimal("100.00"))
        invoice, _ = self.issue_invoice()
        self.assertEqual(calculate_invoice_amounts(self.booking, invoice)["outstanding"], Decimal("200.00"))
        checkout_url = reverse("operations:complete_checkout", args=[self.booking.pk])
        self.client.force_login(self.manager)
        invoice_page = self.client.get(reverse("operations:final_invoice", args=[self.booking.pk]))
        self.assertContains(invoice_page, "outstanding balance of INR 200.00")
        self.assertContains(invoice_page, "Pay outstanding balance")
        self.client.force_login(self.guest)
        forbidden = self.client.post(checkout_url)
        self.assertEqual(forbidden.status_code, 403)
        self.client.force_login(self.manager)
        blocked = self.client.post(checkout_url)
        self.assertEqual(blocked.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_IN)
        Payment.objects.create(
            booking=self.booking, invoice=invoice, kind=Payment.Kind.SERVICE,
            amount=Decimal("200.00"), status=Payment.Status.SUCCESS,
            provider_order_id="settle_order", provider_payment_id="settle_payment",
        )
        update_invoice_status(invoice)
        completed = self.client.post(checkout_url)
        self.assertEqual(completed.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_OUT)
        repeated = self.client.post(checkout_url)
        self.assertEqual(repeated.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_OUT)
        with self.assertRaises(ValidationError):
            self.booking.transition_to(Booking.Status.CHECKED_IN)
