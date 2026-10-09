from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/route/", views.route_api, name="route"),
    path("api/route/map/", views.route_map, name="route-map"),
    path("api/route/gpx/", views.route_gpx, name="route-gpx"),
    path("api/route/warm/", views.route_warm, name="route-warm"),
]
