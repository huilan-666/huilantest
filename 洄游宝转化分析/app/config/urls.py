from django.urls import path, re_path
from django.views.static import serve
from django.conf import settings
from core import views as v
from core import workbench_views as w

urlpatterns = [
    path('', v.diagnosis), path('login/', v.login_page), path('logout/', v.logout_view),
    path('accounts/', v.accounts), path('api/accounts/', v.save_account),
    path('imports/', v.imports), path('api/imports/upload/', v.upload), path('api/imports/<int:pk>/preview/', v.remap), path('api/imports/<int:pk>/publish/', v.publish_view), path('api/batches/<int:pk>/identity/', v.identity_review),
    path('api/crm/status/', v.crm_status), path('api/crm/check/', v.crm_check), path('api/crm/refresh/', v.crm_refresh),
    path('diagnosis/', v.diagnosis), path('api/diagnosis/', v.diagnosis),
    path('api/workbench/', w.overview), path('cases/<int:pk>/', w.detail), path('api/cases/<int:pk>/', w.detail),
    path('api/cases/<int:pk>/assessments/', w.assessment), path('api/cases/<int:pk>/department/', w.department),
    path('api/cases/<int:pk>/resources/', w.resource), path('api/resources/<int:pk>/events/', w.event),
    path('opportunities/<int:pk>/', v.opportunity), path('api/opportunities/<int:pk>/', v.opportunity), path('api/opportunities/<int:pk>/reasons/', v.save_reason),
    path('actions/', v.actions), path('api/actions/', v.actions), path('actions/new/', v.action_detail), path('actions/<int:pk>/', v.action_detail), path('api/actions/create/', v.save_action), path('api/actions/<int:pk>/save/', v.save_action), path('api/actions/<int:pk>/reviews/', v.save_review),
    path('experiences/', v.experiences), path('api/experiences/', v.experiences), path('api/experiences/create/', v.save_experience), path('api/experiences/<int:pk>/save/', v.save_experience), path('api/experiences/<int:pk>/confirm/', v.confirm_experience),
    re_path(r'^assets/(?P<path>.*)$', serve, {'document_root': settings.BASE_DIR/'static'}),
]
