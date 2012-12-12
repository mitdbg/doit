from django.conf.urls.defaults import *
from django.conf import settings

# Uncomment the next two lines to enable the admin:
# from django.contrib import admin
# admin.autodiscover()

urlpatterns = patterns('',
    # Example:
    # (r'^doitweb/', include('doitweb.foo.urls')),

    # doit gui (first GUI)
    (r'^%sdoit/' % settings.ALT_ROOT, include('doit.urls', namespace='doit')),

    # data tamer console
    (r'^%stamer/' % settings.ALT_ROOT, include('tamer.urls', namespace='tamer')),

    # Uncomment the admin/doc line below to enable admin documentation:
    # (r'^admin/doc/', include('django.contrib.admindocs.urls')),

    # Uncomment the next line to enable the admin:
    # (r'^admin/', include(admin.site.urls)),
)
