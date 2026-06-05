from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Booking, CreditTransaction, Receipt, AvailabilityOverride, CustomTime, StudentNote, ActiveLesson, SiteSettings

admin.site.register(User, UserAdmin)
admin.site.register(Booking)
admin.site.register(CreditTransaction)
admin.site.register(Receipt)
admin.site.register(AvailabilityOverride)
admin.site.register(CustomTime)
admin.site.register(StudentNote)
admin.site.register(ActiveLesson)
admin.site.register(SiteSettings)
