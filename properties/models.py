from django.db import models

from django.db import models


class RoomType(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField()
    price_per_night = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    capacity = models.PositiveIntegerField(default=2)
    image = models.ImageField(
        upload_to="rooms/",
        blank=True,
        null=True,
    )

    def __str__(self):
        return self.name


class Room(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        MAINTENANCE = "MAINTENANCE", "Maintenance"

    room_number = models.CharField(max_length=20, unique=True)
    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.PROTECT,
        related_name="rooms",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
    )
    floor = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.room_number} - {self.room_type.name}"


class Service(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name