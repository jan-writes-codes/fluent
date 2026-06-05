from django.urls import path
from . import views

urlpatterns = [
    path('', views.app_view, name='app'),
    path('api/login/', views.api_login),
    path('api/logout/', views.api_logout),
    path('api/bookings/', views.api_bookings),
    path('api/bookings/<int:pk>/', views.api_booking_detail),
    path('api/credits/<str:slug>/', views.api_credits),
    path('api/users/me/billing/', views.api_billing),
    path('api/availability/', views.api_availability),
    path('api/custom-times/', views.api_custom_times),
    path('api/notes/<str:slug>/', views.api_notes),
    path('api/lessons/<str:slug>/', views.api_lessons),
    path('api/users/', views.api_users),
    path('api/users/<str:slug>/', views.api_user_detail),
    path('api/settings/', views.api_settings),
]
