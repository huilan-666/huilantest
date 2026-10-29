import json
import secrets
from pathlib import Path
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from core.models import Profile


class Command(BaseCommand):
    help='建立本地试用账号；随机密码只写入本地凭据文件，不写日志。'

    def handle(self,*args,**options):
        path=Path(settings.RUNTIME)/'本地登录凭据.json'
        credentials=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        entries=[('ops.huilan','蕙兰','ops',[]),('leader.one','业务一部负责人','business',['业务一部']),('leader.two','业务二部负责人','business',['业务二部']),('ceo','CEO','ceo',[])]
        for username,name,role,departments in entries:
            user=get_user_model().objects.filter(username=username).first()
            if not user:
                password=secrets.token_urlsafe(16)
                user=get_user_model().objects.create_user(username=username,password=password,first_name=name)
                Profile.objects.create(user=user,role=role,departments=departments)
                credentials[username]={'姓名':name,'角色':role,'密码':password}
        path.write_text(json.dumps(credentials,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        self.stdout.write('本地账号已就绪。随机凭据保存在 runtime/本地登录凭据.json；未创建业务批次、原因或行动。')
