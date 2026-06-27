from django.urls import path
from . import views

urlpatterns = [
    path('', views.landing_view, name='landing'),
    path('app/', views.app_view, name='app'),
    path('login/', views.login_view, name='login'),
    path('impressum/', views.impressum_view, name='impressum'),
    path('datenschutz/', views.datenschutz_view, name='datenschutz'),
    path('api/login/', views.api_login),
    path('api/logout/', views.api_logout),
    path('api/bookings/', views.api_bookings),
    path('api/bookings/<int:pk>/', views.api_booking_detail),
    path('api/credits/<str:slug>/', views.api_credits),
    path('api/checkout/', views.api_checkout),
    path('api/checkout/confirm/', views.api_checkout_confirm),
    path('api/stripe/webhook/', views.api_stripe_webhook),
    path('api/users/me/billing/', views.api_billing),
    path('api/availability/', views.api_availability),
    path('api/custom-times/', views.api_custom_times),
    path('api/notes/<str:slug>/', views.api_notes),
    path('api/lessons/<str:slug>/', views.api_lessons),
    path('api/lesson-files/download/<int:file_id>/', views.api_lesson_file_download),
    path('api/lesson-files/<int:file_id>/', views.api_lesson_file_detail),
    path('api/lesson-files/<str:lesson_id>/', views.api_lesson_files),
    path('api/users/', views.api_users),
    path('api/users/<str:slug>/', views.api_user_detail),
    path('api/settings/', views.api_settings),
]
