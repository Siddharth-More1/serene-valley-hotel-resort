from django.urls import path

from . import views

app_name = "operations"

urlpatterns = [
    path("manager/", views.manager_dashboard, name="dashboard"),
    path("bookings/<int:booking_id>/services/", views.service_request, name="service_request"),
    path("staff/services/", views.staff_service_board, name="service_board"),
    path("staff/service-requests/<int:request_id>/update/", views.update_service_request, name="service_request_update"),
    path("staff/bookings/<int:booking_id>/services/add/", views.add_folio_item, name="add_folio_item"),
    path("staff/folio-items/<int:item_id>/reverse/", views.reverse_folio_item, name="reverse_folio_item"),
    path("staff/bookings/<int:booking_id>/final-invoice/", views.final_invoice, name="final_invoice"),
    path("staff/bookings/<int:booking_id>/final-invoice/issue/", views.issue_final_invoice, name="issue_final_invoice"),
    path("invoices/<int:invoice_id>/", views.invoice_detail, name="invoice"),
    path("invoices/<int:invoice_id>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("staff/bookings/<int:booking_id>/checkout/", views.complete_checkout, name="complete_checkout"),
]
