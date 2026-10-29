from django.conf import settings
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=[('ops', '运营'), ('business', '业务负责人'), ('ceo', 'CEO')])
    departments = models.JSONField(default=list)
    version = models.PositiveIntegerField(default=1)


class ImportPreview(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    filename = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64)
    file_path = models.CharField(max_length=500)
    sheets = models.JSONField(default=list)
    sheet = models.CharField(max_length=200)
    headers = models.JSONField(default=list)
    raw_rows = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    config = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class Batch(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64, unique=True)
    file_hash = models.CharField(max_length=64)
    source_path = models.CharField(max_length=500)
    sheet = models.CharField(max_length=200)
    config = models.JSONField(default=dict)
    mapping = models.JSONField(default=dict)
    counts = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    previous = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Record(models.Model):
    batch = models.ForeignKey(Batch, related_name='records', on_delete=models.PROTECT)
    row_number = models.PositiveIntegerField()
    raw = models.JSONField(default=dict)
    data = models.JSONField(default=dict)
    department = models.CharField(max_length=200)
    opportunity_id = models.CharField(max_length=200, blank=True)
    customer_id = models.CharField(max_length=200, blank=True)
    identity = models.CharField(max_length=20, default='unknown')
    identity_evidence = models.TextField(blank=True)
    unique_link = models.BooleanField(default=False)
    excluded = models.BooleanField(default=False)
    exclusion_reason = models.TextField(blank=True)
    issues = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['batch', 'row_number'], name='batch_row_unique')]


class Reason(models.Model):
    record = models.ForeignKey(Record, related_name='reasons', on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    verdict = models.CharField(max_length=20, default='pending')
    evidence = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class Action(models.Model):
    record = models.ForeignKey(Record, on_delete=models.PROTECT)
    department = models.CharField(max_length=200)
    opportunity_id = models.CharField(max_length=200, blank=True)
    customer_id = models.CharField(max_length=200, blank=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='created_actions', on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='assigned_actions', on_delete=models.PROTECT)
    task = models.TextField()
    due_date = models.DateField()
    second_review_date = models.DateField()
    status = models.CharField(max_length=30, default='pending')
    note = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Review(models.Model):
    action = models.ForeignKey(Action, related_name='reviews', on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    round = models.PositiveSmallIntegerField()
    note = models.TextField()
    execution = models.TextField()
    baseline = models.ForeignKey(Batch, related_name='baseline_reviews', null=True, blank=True, on_delete=models.PROTECT)
    followup = models.ForeignKey(Batch, related_name='followup_reviews', null=True, blank=True, on_delete=models.PROTECT)
    window = models.PositiveIntegerField(default=30)
    comparison = models.JSONField(default=dict)
    source_cutoff = models.DateTimeField(null=True, blank=True)
    exported_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Experience(models.Model):
    root_id = models.UUIDField()
    version = models.PositiveIntegerField(default=1)
    editor_version = models.PositiveIntegerField(default=1)
    previous = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT)
    department = models.CharField(max_length=200)
    condition = models.TextField()
    cause = models.TextField()
    evidence = models.TextField()
    task = models.TextField()
    result = models.TextField(blank=True)
    evidence_status = models.CharField(max_length=20, default='unverified')
    publication = models.CharField(max_length=20, default='draft')
    validation_design = models.TextField(blank=True)
    validation_result = models.TextField(blank=True)
    references = models.ManyToManyField(Record, blank=True)
    action = models.ForeignKey(Action, null=True, blank=True, on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='authored_experiences', on_delete=models.PROTECT)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, related_name='confirmed_experiences', on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['root_id', 'version'], name='experience_version_unique')]


class Audit(models.Model):
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    entity = models.CharField(max_length=40)
    entity_id = models.PositiveIntegerField()
    department = models.CharField(max_length=200, blank=True)
    event = models.CharField(max_length=60)
    version = models.PositiveIntegerField(default=1)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class CRMReadRun(models.Model):
    kind = models.CharField(max_length=10)
    request_id = models.CharField(max_length=32, unique=True)
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default='running')
    error_code = models.CharField(max_length=30, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=200, blank=True)
    source_cutoff = models.DateTimeField(null=True, blank=True)
    coverage = models.TextField(blank=True)
    returned_count = models.PositiveIntegerField(null=True, blank=True)
    batch = models.ForeignKey(Batch, null=True, blank=True, on_delete=models.PROTECT)
    reused = models.BooleanField(default=False)
    case_snapshot = models.ForeignKey('CaseSnapshot', null=True, blank=True, on_delete=models.PROTECT)
    data_type = models.CharField(max_length=10, default='table')
    observed_on = models.DateField(null=True, blank=True)


class CRMConnection(models.Model):
    connection_key = models.CharField(max_length=30, unique=True, default='default')
    current_run = models.ForeignKey(CRMReadRun, null=True, blank=True, on_delete=models.PROTECT)
    lease_token = models.CharField(max_length=32, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    check_status = models.CharField(max_length=20, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)


class CaseSnapshot(models.Model):
    source = models.CharField(max_length=200)
    source_label = models.CharField(max_length=200)
    observed_on = models.DateField()
    source_cutoff = models.DateTimeField(null=True, blank=True)
    time_precision = models.CharField(max_length=10, default='day')
    coverage = models.TextField()
    version = models.PositiveIntegerField(default=1)
    fingerprint = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'version'], name='case_source_version_unique')]


class BusinessCase(models.Model):
    source = models.CharField(max_length=200)
    source_key = models.CharField(max_length=200)
    current_snapshot = models.ForeignKey(CaseSnapshot, related_name='current_cases', on_delete=models.PROTECT)
    first_snapshot = models.ForeignKey(CaseSnapshot, related_name='first_cases', on_delete=models.PROTECT)
    data = models.JSONField(default=dict)
    department = models.CharField(max_length=200, blank=True)
    department_override = models.CharField(max_length=200, null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'source_key'], name='business_source_key_unique')]


class CaseAssessment(models.Model):
    case = models.ForeignKey(BusinessCase, related_name='assessments', on_delete=models.PROTECT)
    target = models.CharField(max_length=20)
    category = models.CharField(max_length=30, blank=True)
    verdict = models.CharField(max_length=20)
    value = models.JSONField(default=dict)
    evidence = models.TextField()
    occurred_on = models.DateField()
    based_snapshot = models.ForeignKey(CaseSnapshot, on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ResourceRequest(models.Model):
    case = models.ForeignKey(BusinessCase, related_name='resources', on_delete=models.PROTECT)
    source_snapshot = models.ForeignKey(CaseSnapshot, on_delete=models.PROTECT)
    problem = models.TextField()
    resource_type = models.CharField(max_length=20)
    owner = models.CharField(max_length=200)
    requested_on = models.DateField()
    next_step = models.TextField()
    expected_feedback = models.TextField()
    first_review_on = models.DateField()
    second_review_on = models.DateField()
    version = models.PositiveIntegerField(default=1)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ResourceEvent(models.Model):
    resource = models.ForeignKey(ResourceRequest, related_name='events', on_delete=models.PROTECT)
    kind = models.CharField(max_length=20)
    occurred_on = models.DateField()
    actor = models.CharField(max_length=200)
    evidence = models.TextField()
    payload = models.JSONField(default=dict)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
