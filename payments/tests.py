from django.contrib import admin
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from decimal import Decimal
import json
from unittest.mock import patch
import razorpay

from accounts.models import User
from bookings.models import Booking
from properties.models import Room, RoomType
from operations.models import CheckoutInvoice
from operations.billing import calculate_invoice_amounts
from .admin import PaymentAdmin
from .models import Payment, Receipt


class PaymentAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.manager = User.objects.create_user(
            username="manager",
            password="StrongPass!9284",
            role=User.Role.MANAGER,
            is_staff=True,
        )
        self.receptionist = User.objects.create_user(
            username="desk",
            password="StrongPass!9284",
            role=User.Role.RECEPTIONIST,
            is_staff=True,
        )
        self.customer = User.objects.create_user(
            username="guest",
            password="StrongPass!9284",
        )
        self.model_admin = PaymentAdmin(Payment, admin.site)

    def test_only_managers_can_view_payments_and_nobody_can_edit_them(self):
        request = self.factory.get("/admin/")
        request.user = self.manager
        self.assertTrue(self.model_admin.has_module_permission(request))
        self.assertTrue(self.model_admin.has_view_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))
        self.assertFalse(self.model_admin.has_add_permission(request))
        self.assertFalse(self.model_admin.has_delete_permission(request))

        for user in (self.receptionist, self.customer):
            request.user = user
            self.assertFalse(self.model_admin.has_module_permission(request))
            self.assertFalse(self.model_admin.has_view_permission(request))

    def test_admin_summary_hides_provider_identifiers(self):
        admin_fields = set(self.model_admin.fields)
        list_fields = set(self.model_admin.list_display)
        search_fields = set(self.model_admin.search_fields)
        for sensitive_field in ("provider_order_id", "provider_payment_id"):
            self.assertNotIn(sensitive_field, admin_fields)
            self.assertNotIn(sensitive_field, list_fields)
            self.assertNotIn(sensitive_field, search_fields)


@override_settings(
    RAZORPAY_KEY_ID="rzp_test_example",
    RAZORPAY_KEY_SECRET="test-secret",
    RAZORPAY_WEBHOOK_SECRET="webhook-test-secret",
)
class RazorpayWorkflowTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username="booker", password="StrongPass!9284"
        )
        self.other_customer = User.objects.create_user(
            username="other", password="StrongPass!9284"
        )
        room_type = RoomType.objects.create(
            name="Valley Suite",
            description="Suite",
            price_per_night=Decimal("125.50"),
            capacity=2,
        )
        room = Room.objects.create(room_number="T-101", room_type=room_type)
        self.booking = Booking.objects.create(
            customer=self.customer,
            room=room,
            check_in="2030-05-01",
            check_out="2030-05-03",
            guests=2,
            total_price=Decimal("251.00"),
            status=Booking.Status.CONFIRMED,
        )
        self.client = Client()
        self.client.force_login(self.customer)

    def make_pending_payment(self):
        return Payment.objects.create(
            booking=self.booking,
            amount=self.booking.total_price,
            provider_order_id="order_test_123",
            status=Payment.Status.PENDING,
        )

    def provider_entities(self, payment_id="pay_test_123", *, status="captured", amount=25100):
        return (
            {"id": "order_test_123", "amount": 25100, "currency": "INR"},
            {
                "id": payment_id,
                "order_id": "order_test_123",
                "amount": amount,
                "currency": "INR",
                "status": status,
            },
        )

    def verify_response(self, payment_id="pay_test_123"):
        return self.client.post(
            reverse("payments:verify", args=[Payment.objects.get(booking=self.booking).id]),
            {
                "razorpay_order_id": "order_test_123",
                "razorpay_payment_id": payment_id,
                "razorpay_signature": "valid-signature",
            },
        )

    @patch("payments.views.get_razorpay_client")
    def test_creates_order_using_server_booking_total_and_reuses_it(self, get_client):
        client = get_client.return_value
        client.order.create.return_value = {
            "id": "order_test_123", "amount": 25100, "currency": "INR"
        }
        url = reverse("payments:create", args=[self.booking.id])

        first = self.client.post(url)
        self.assertRedirects(
            first,
            reverse("payments:checkout", args=[Payment.objects.get(booking=self.booking).id]),
            fetch_redirect_response=False,
        )
        client.order.create.assert_called_once_with({
            "amount": 25100,
            "currency": "INR",
            "receipt": f"payment_{Payment.objects.get(booking=self.booking).id}",
        })

        second = self.client.post(url, {"amount": "1"})
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), 1)
        client.order.create.assert_called_once()

    @patch("payments.views.get_razorpay_client")
    def test_order_setup_failure_is_logged_without_provider_details(self, get_client):
        get_client.return_value.order.create.side_effect = RuntimeError(
            "provider response must not be copied into application logs"
        )
        with self.assertLogs("payments.views", level="ERROR") as captured:
            response = self.client.post(reverse("payments:create", args=[self.booking.id]))

        payment = Payment.objects.get(booking=self.booking)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertIn("exception_type=RuntimeError", captured.output[0])
        self.assertIn("category=sdk_or_unexpected_error", captured.output[0])
        self.assertNotIn("provider response", captured.output[0])

    def test_customer_cannot_create_or_verify_another_customers_payment(self):
        payment = self.make_pending_payment()
        self.client.force_login(self.other_customer)

        create = self.client.post(reverse("payments:create", args=[self.booking.id]))
        verify = self.client.post(
            reverse("payments:verify", args=[payment.id]),
            {
                "razorpay_order_id": payment.provider_order_id,
                "razorpay_payment_id": "pay_test_123",
                "razorpay_signature": "signature",
            },
        )
        self.assertEqual(create.status_code, 404)
        self.assertEqual(verify.status_code, 404)

    def test_browser_payment_verification_requires_csrf(self):
        payment = self.make_pending_payment()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.customer)
        response = csrf_client.post(
            reverse("payments:verify", args=[payment.id]),
            {
                "razorpay_order_id": payment.provider_order_id,
                "razorpay_payment_id": "pay_test_123",
                "razorpay_signature": "signature",
            },
        )
        self.assertEqual(response.status_code, 403)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    @patch("payments.views.get_razorpay_client")
    def test_invalid_signature_does_not_mark_payment_success(self, get_client):
        payment = self.make_pending_payment()
        client = get_client.return_value
        client.utility.verify_payment_signature.side_effect = (
            razorpay.errors.SignatureVerificationError("bad signature")
        )

        response = self.verify_response()
        payment.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertFalse(Receipt.objects.filter(payment=payment).exists())

    @patch("payments.views.get_razorpay_client")
    def test_amount_mismatch_does_not_mark_payment_success(self, get_client):
        payment = self.make_pending_payment()
        client = get_client.return_value
        order, provider_payment = self.provider_entities(amount=25099)
        client.order.fetch.return_value = order
        client.payment.fetch.return_value = provider_payment

        response = self.verify_response()
        payment.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payment.status, Payment.Status.PENDING)

    @patch("payments.views.get_razorpay_client")
    def test_failed_provider_payment_is_not_recorded_as_success(self, get_client):
        payment = self.make_pending_payment()
        client = get_client.return_value
        order, provider_payment = self.provider_entities(status="failed")
        client.order.fetch.return_value = order
        client.payment.fetch.return_value = provider_payment

        response = self.verify_response()
        payment.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertFalse(Receipt.objects.filter(payment=payment).exists())

    @patch("payments.views.get_razorpay_client")
    def test_successful_verification_is_idempotent(self, get_client):
        payment = self.make_pending_payment()
        client = get_client.return_value
        client.order.fetch.return_value, client.payment.fetch.return_value = self.provider_entities()
        client.payment.fetch.return_value["method"] = "card"

        first = self.verify_response()
        second = self.verify_response()
        payment.refresh_from_db()
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(payment.provider_payment_id, "pay_test_123")
        self.assertEqual(Payment.objects.filter(booking=self.booking, status=Payment.Status.SUCCESS).count(), 1)
        self.assertEqual(Receipt.objects.filter(payment=payment).count(), 1)
        receipt = Receipt.objects.get(payment=payment)
        self.assertEqual(receipt.details["amount"], "251.00")
        self.assertEqual(receipt.details["currency"], "INR")
        self.assertEqual(receipt.details["nights"], 2)
        self.assertEqual(receipt.details["payment_method"], "card")
        self.assertTrue(receipt.receipt_number.startswith("SVHR-"))
        client.payment.fetch.assert_called_once_with("pay_test_123")

    @patch("payments.views.get_razorpay_client")
    def test_receipt_page_pdf_and_customer_ownership(self, get_client):
        payment = self.make_pending_payment()
        client = get_client.return_value
        order, provider_payment = self.provider_entities()
        provider_payment["method"] = "card"
        client.order.fetch.return_value = order
        client.payment.fetch.return_value = provider_payment
        self.verify_response()
        receipt = Receipt.objects.get(payment=payment)

        page = self.client.get(reverse("payments:receipt", args=[receipt.pk]))
        pdf = self.client.get(reverse("payments:receipt_pdf", args=[receipt.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "PAYMENT RECEIPT")
        self.assertContains(page, receipt.receipt_number)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF-"))
        self.assertIn(receipt.receipt_number, pdf["Content-Disposition"])

        self.client.force_login(self.other_customer)
        self.assertEqual(
            self.client.get(reverse("payments:receipt", args=[receipt.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("payments:receipt_pdf", args=[receipt.pk])).status_code,
            404,
        )

    def test_anonymous_customer_must_sign_in_to_view_receipt(self):
        payment = self.make_pending_payment()
        self.assertFalse(Receipt.objects.filter(payment=payment).exists())
        response = Client().get(reverse("payments:receipt", args=[payment.pk]))
        self.assertEqual(response.status_code, 302)

    @patch("payments.views.get_razorpay_client")
    def test_webhook_signature_and_repeated_capture_are_safe(self, get_client):
        payment = self.make_pending_payment()
        client = get_client.return_value
        client.order.fetch.return_value, client.payment.fetch.return_value = self.provider_entities()
        payload = json.dumps({
            "event": "payment.captured",
            "payload": {"payment": {"entity": {
                "id": "pay_test_123", "order_id": "order_test_123"
            }}}
        })
        url = reverse("payments:webhook")

        client.utility.verify_webhook_signature.side_effect = (
            razorpay.errors.SignatureVerificationError("invalid")
        )
        invalid = self.client.post(
            url, data=payload, content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="bad",
        )
        self.assertEqual(invalid.status_code, 400)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

        client.utility.verify_webhook_signature.side_effect = None
        first = self.client.post(
            url, data=payload, content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid",
        )
        second = self.client.post(
            url, data=payload, content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid",
        )
        payment.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(Payment.objects.filter(booking=self.booking, status=Payment.Status.SUCCESS).count(), 1)

    def make_issued_invoice(self):
        invoice = CheckoutInvoice.objects.create(
            booking=self.booking,
            invoice_number="SVHR-INV-2030-000001",
            room_charges=Decimal("251.00"),
            service_charges=Decimal("200.00"),
            total_charges=Decimal("451.00"),
            status=CheckoutInvoice.Status.ISSUED,
        )
        Payment.objects.create(
            booking=self.booking, kind=Payment.Kind.ROOM,
            amount=Decimal("251.00"), status=Payment.Status.SUCCESS,
            provider_order_id="room_order_for_invoice", provider_payment_id="room_pay_for_invoice",
        )
        return invoice

    @patch("payments.views.get_razorpay_client")
    def test_service_payment_uses_outstanding_balance_and_receipt_is_amount_received(self, get_client):
        invoice = self.make_issued_invoice()
        client = get_client.return_value
        client.order.create.return_value = {"id": "order_service_123", "amount": 20000, "currency": "INR"}
        create = self.client.post(reverse("payments:invoice_create", args=[invoice.pk]))
        payment = Payment.objects.get(invoice=invoice, kind=Payment.Kind.SERVICE)
        self.assertRedirects(create, reverse("payments:checkout", args=[payment.pk]), fetch_redirect_response=False)
        client.order.create.assert_called_once_with({
            "amount": 20000, "currency": "INR", "receipt": f"invoice_{invoice.pk}_payment_{payment.pk}"
        })

        client.order.fetch.return_value = {"id": "order_service_123", "amount": 20000, "currency": "INR"}
        client.payment.fetch.return_value = {
            "id": "pay_service_123", "order_id": "order_service_123", "amount": 20000,
            "currency": "INR", "status": "captured", "method": "card",
        }
        callback = self.client.post(reverse("payments:verify", args=[payment.pk]), {
            "razorpay_order_id": "order_service_123",
            "razorpay_payment_id": "pay_service_123",
            "razorpay_signature": "valid",
        })
        self.assertRedirects(callback, reverse("operations:invoice", args=[invoice.pk]), fetch_redirect_response=False)
        payment.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(invoice.status, CheckoutInvoice.Status.PAID)
        self.assertEqual(calculate_invoice_amounts(invoice.booking, invoice)["outstanding"], Decimal("0.00"))
        receipt = Receipt.objects.get(payment=payment)
        self.assertEqual(receipt.details["amount_received"], "200.00")
        self.assertNotEqual(receipt.details["amount_received"], "451.00")

        repeat = self.client.post(reverse("payments:verify", args=[payment.pk]), {
            "razorpay_order_id": "order_service_123",
            "razorpay_payment_id": "pay_service_123",
            "razorpay_signature": "valid",
        })
        self.assertEqual(repeat.status_code, 302)
        self.assertEqual(Receipt.objects.filter(payment=payment).count(), 1)
        pdf = self.client.get(reverse("payments:receipt_pdf", args=[receipt.pk]))
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF-"))

        self.client.force_login(self.other_customer)
        self.assertEqual(self.client.get(reverse("payments:receipt", args=[receipt.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("payments:receipt_pdf", args=[receipt.pk])).status_code, 404)

    @patch("payments.views.get_razorpay_client")
    def test_service_payment_owner_and_signature_are_checked_before_receipt(self, get_client):
        invoice = self.make_issued_invoice()
        payment = Payment.objects.create(
            booking=self.booking, invoice=invoice, kind=Payment.Kind.SERVICE,
            amount=Decimal("200.00"), provider_order_id="order_service_bad", status=Payment.Status.PENDING,
        )
        self.client.force_login(self.other_customer)
        response = self.client.post(reverse("payments:invoice_create", args=[invoice.pk]))
        self.assertEqual(response.status_code, 404)

        self.client.force_login(self.customer)
        client = get_client.return_value
        client.utility.verify_payment_signature.side_effect = razorpay.errors.SignatureVerificationError("invalid")
        response = self.client.post(reverse("payments:verify", args=[payment.pk]), {
            "razorpay_order_id": "order_service_bad",
            "razorpay_payment_id": "pay_service_bad",
            "razorpay_signature": "bad",
        })
        payment.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertFalse(Receipt.objects.filter(payment=payment).exists())
