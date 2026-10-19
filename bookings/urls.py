
from django.urls import path
from . import views

app_name = "bookings"

urlpatterns = [
    path("create/", views.create_booking, name="create"),
    path("my-bookings/", views.my_bookings, name="my_bookings"),
    path("<int:booking_id>/", views.booking_detail, name="detail"),
    path(
        "<int:booking_id>/cancel/",
        views.cancel_booking,
        name="cancel",
    ),
]
