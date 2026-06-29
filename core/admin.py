import json

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Booking, CreditTransaction, Receipt, AvailabilityOverride, CustomTime, StudentNote, ActiveLesson, SiteSettings


class SiteSettingsForm(forms.ModelForm):
    """Renders `popular_n` as a dropdown of the currently configured pack sizes
    so the "Beliebt" badge can be moved between price packages without touching
    raw JSON. Choices are rebuilt from packs_json each time the form loads."""

    class Meta:
        model = SiteSettings
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(0, "— Keiner —")]
        raw = (self.instance.packs_json if self.instance and self.instance.pk
               else self.fields["packs_json"].initial) or ""
        try:
            for p in json.loads(raw):
                n = int(p.get("n"))
                label = f"{n} Einheit" if n == 1 else f"{n} Einheiten"
                price = (p.get("price") or "").strip()
                choices.append((n, f"{label}{f' ({price})' if price else ''}"))
        except (ValueError, TypeError):
            pass
        # Preserve a saved value that no longer matches any pack.
        current = self.instance.popular_n if self.instance else None
        if current and current not in [c[0] for c in choices]:
            choices.append((current, f"{current} Einheiten"))
        self.fields["popular_n"] = forms.TypedChoiceField(
            choices=choices, coerce=int, required=True, label="Beliebt-Markierung",
            help_text="Welches Paket auf der Preisseite als „Beliebt“ hervorgehoben wird.",
        )


class SiteSettingsAdmin(admin.ModelAdmin):
    form = SiteSettingsForm


admin.site.register(User, UserAdmin)
admin.site.register(Booking)
admin.site.register(CreditTransaction)
admin.site.register(Receipt)
admin.site.register(AvailabilityOverride)
admin.site.register(CustomTime)
admin.site.register(StudentNote)
admin.site.register(ActiveLesson)
admin.site.register(SiteSettings, SiteSettingsAdmin)
