import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.services import DomainError
from core.workbench import publish_cases


class Command(BaseCommand):
    help = '导入合成演示案例（非真实客户）；重复不覆盖本地核实或创建资源。'

    def handle(self, *args, **options):
        source = Path(__file__).resolve().parents[2] / 'data' / 'historical_cases_20261028.json'
        try:
            snapshot, created = publish_cases(json.loads(source.read_text(encoding='utf-8')))
        except (OSError, ValueError, DomainError) as exc:
            raise CommandError('历史案例文件读取或发布失败，请检查输入。') from exc
        self.stdout.write(self.style.SUCCESS(f'历史案例快照 {snapshot.pk} v{snapshot.version}，' + ('已导入' if created else '复用原版；本地事实未覆盖')))
