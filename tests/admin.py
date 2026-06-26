from django.contrib.admin import AdminSite

custom_site = AdminSite(name="custom")
other_site = AdminSite(name="other")
gated_site = AdminSite(name="gated")
