
from django.urls import path
from . import views

app_name = "payments"

urlpatterns = [
    path(
        "create/<int:booking_id>/",
        views.create_payment,
        name="create",
    ),
    path("invoice/create/<int:invoice_id>/", views.create_invoice_payment, name="invoice_create"),
    path(
        "checkout/<int:payment_id>/",
        views.checkout,
        name="checkout",
    ),
    
path(
    "verify/<int:payment_id>/",
    views.verify_payment,
    name="verify",
),
    path("razorpay/webhook/", views.razorpay_webhook, name="webhook"),
    path("receipt/<int:receipt_id>/", views.receipt_detail, name="receipt"),
    path("receipt/<int:receipt_id>/pdf/", views.receipt_pdf, name="receipt_pdf"),
]
