from django.db import models


class Station(models.Model):
    """A truck stop with its (lowest listed) retail price and a map location of known precision."""

    LOCATION_SOURCES = [
        ("site", "matched to its fuel station (OpenStreetMap or Overture Maps)"),
        ("uncertain", "matched to a fuel station, but the two sources disagree on which one"),
        ("exit", "the stop's highway exit on OpenStreetMap"),
        ("city", "centre of its city (no better match)"),
    ]

    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    latitude = models.FloatField()
    longitude = models.FloatField()
    location_source = models.CharField(max_length=10, choices=LOCATION_SOURCES, default="city")
    price = models.FloatField(help_text="USD per gallon")

    class Meta:
        indexes = [models.Index(fields=["state"])]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price:.3f}"
