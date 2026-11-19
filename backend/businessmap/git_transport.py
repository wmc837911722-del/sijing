"""Bounded Git transport. Secrets live only in OS credentials or process memory."""
from __future__ import annotations

import base64
import ipaddress
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urlsplit

from .core import CoreError
from .providers import Secrets


class GitError(CoreError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(status_code, message)
        self.code = code


class GitTransport:
    FIELDS = ('auth_type', 'username', 'proxy_url', 'ca_bundle', 'ssh_key_path', 'known_hosts_path')
    SCP = re.compile(r'^[\w.][\w.-]*@([\w.-]+):(.+)$')

    def __init__(self, store):
        self.store = store
        self.secrets = Secrets(store.data_dir)

    @classmethod
    def is_ssh(cls, url: str) -> bool:
        return url.startswith('ssh://') or bool(cls.SCP.fullmatch(url))

    def endpoint(self, url: str) -> tuple[str, bool]:
        url = url.strip()
        if not url or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise GitError('invalid_configuration', '请输入有效的本地目录或 Git 地址。')
        # Preserve SCP paths exactly: relative paths are relative to the SSH home,
        # while GitLab/Gitea wrappers may also distinguish "group" from "~/group".
        if self.SCP.fullmatch(url):
            return url, True
        if '://' not in url:
            return str(Path(url).expanduser().resolve()), False
        try:
            parts = urlsplit(url)
            hostname, port = parts.hostname, parts.port
        except ValueError:
            raise GitError('invalid_configuration', 'Git 地址格式无效。') from None
        loopback = hostname == 'localhost'
        try:
            loopback = loopback or ipaddress.ip_address(hostname or '').is_loopback
        except ValueError:
            pass
        if parts.scheme not in ('https', 'ssh') and not (parts.scheme == 'http' and loopback):
            raise GitError('invalid_configuration', '远程仓库使用 HTTPS 或 SSH；明文 HTTP 仅允许本机测试。')
        if not hostname or parts.password is not None or parts.query or parts.fragment:
            raise GitError('invalid_configuration', 'Git 地址不能包含密码、查询参数或片段。')
        if parts.scheme != 'ssh' and parts.username is not None:
            raise GitError('invalid_configuration', '请在认证栏填写用户名和 Token，不要放在仓库地址中。')
        if parts.scheme == 'ssh' and (not re.fullmatch(r'[\w.][\w.-]*', parts.username or 'git')
                                      or not re.fullmatch(r'[\w.:-]+', hostname)):
            raise GitError('invalid_configuration', 'SSH 主机或用户名格式无效。')
        return url, True

    def normalize(self, url: str, value: dict | None) -> dict:
        url, remote = self.endpoint(url)
        value = value or {}
        kind = value.get('auth_type') or ('ssh-agent' if self.is_ssh(url) else 'none')
        if kind not in ('none', 'https', 'ssh-agent', 'ssh-key'):
            raise GitError('invalid_configuration', '不支持的 Git 认证方式。')
        result = {field: str(value.get(field) or '').strip() for field in self.FIELDS}
        result['auth_type'] = kind
        if any(any(ord(c) < 32 or ord(c) == 127 for c in text) for text in result.values()):
            raise GitError('invalid_configuration', '连接设置不能包含控制字符。')
        if not remote:
            if kind != 'none' or any(result[field] for field in self.FIELDS[1:]):
                raise GitError('invalid_configuration', '本地仓库只观察已提交的 HEAD，无需网络认证设置。')
            return result
        ssh = self.is_ssh(url)
        if ssh != kind.startswith('ssh-'):
            raise GitError('invalid_configuration', 'SSH 地址请选择 SSH 认证，HTTPS 地址请选择匿名或账号认证。')
        if kind == 'https' and not result['username']:
            raise GitError('invalid_configuration', '请填写 Git 用户名（Token 的用户名按公司 Git 平台要求填写）。')
        if ':' in result['username']:
            raise GitError('invalid_configuration', '用户名不能包含冒号。')
        if ssh and (result['username'] or result['proxy_url'] or result['ca_bundle']):
            raise GitError('invalid_configuration', 'SSH 用户名写在地址中；HTTPS 代理和 CA 设置不适用于 SSH。')
        if not ssh and (result['ssh_key_path'] or result['known_hosts_path']):
            raise GitError('invalid_configuration', 'SSH 文件设置仅适用于 SSH 地址。')
        if kind == 'ssh-agent' and result['ssh_key_path']:
            raise GitError('invalid_configuration', '使用已有私钥文件时请选择 SSH 私钥认证。')
        if kind == 'ssh-key' and not result['ssh_key_path']:
            raise GitError('invalid_configuration', '请选择已有的 SSH 私钥文件；有口令的私钥请先加载到 SSH Agent。')
        if result['proxy_url']:
            try:
                proxy = urlsplit(result['proxy_url'])
                valid = (proxy.scheme in ('http', 'https', 'socks5', 'socks5h') and proxy.hostname
                         and proxy.username is None and proxy.password is None and not proxy.query
                         and not proxy.fragment and proxy.path in ('', '/'))
                _ = proxy.port
            except ValueError:
                valid = False
            if not valid:
                raise GitError('invalid_configuration', '代理地址必须是不含账号、密码或路径的 HTTP(S)/SOCKS5 地址。')
        for field in ('ca_bundle', 'ssh_key_path', 'known_hosts_path'):
            if result[field]:
                if field != 'ca_bundle' and '%' in result[field]:
                    raise GitError('invalid_configuration', 'SSH 文件路径不能包含会被 OpenSSH 展开的 % 占位符。')
                path = Path(result[field]).expanduser().resolve()
                if not path.is_file():
                    raise GitError('invalid_configuration', f'{ {"ca_bundle":"CA 证书", "ssh_key_path":"SSH 私钥", "known_hosts_path":"known_hosts"}[field]}文件不存在或不可读取。')
                result[field] = str(path)
        return result

    def key(self, repository_id: str) -> str | None:
        return self.store.config_get(f'repository_secret_ref:{repository_id}')

    def public(self, repository: dict) -> dict:
        connection = {field: repository.get('connection', {}).get(field, '') for field in self.FIELDS}
        connection['auth_type'] = connection['auth_type'] or ('ssh-agent' if self.is_ssh(repository['url']) else 'none')
        key = self.key(repository['id']) if connection['auth_type'] == 'https' else None
        return {**connection, 'has_secret': bool(key and self.secrets.get(key)),
                'secret_storage': self.secrets.storage(key) if key else 'none'}

    def resolve_secret(self, connection: dict, supplied: dict | None, repository_id: str | None = None) -> str | None:
        if connection['auth_type'] != 'https':
            return None
        if supplied is not None and supplied.get('secret') is not None:
            secret = supplied['secret']
            if not isinstance(secret, str) or len(secret) > 16384 or any(c in secret for c in ('\r', '\n', '\0')):
                raise GitError('invalid_configuration', 'Token/密码格式无效。')
            return secret
        key = self.key(repository_id) if repository_id else None
        return self.secrets.get(key) if key else None

    def save_secret(self, repository_id: str, connection: dict, supplied: dict | None) -> None:
        old_ref = self.key(repository_id)
        storage_key = f'repository_secret_storage:{repository_id}'
        old_storage = self.store.config_get(storage_key, 'keyring')
        if connection['auth_type'] != 'https':
            # Clearing the reference cannot resurrect an older OS-stored credential.
            self.store.config_set(f'repository_secret_ref:{repository_id}', None)
            self.store.config_set(storage_key, 'none')
        elif supplied is not None and supplied.get('secret') is not None:
            secret = self.resolve_secret(connection, supplied)
            key = f'repository:{repository_id}:{uuid.uuid4().hex}'
            storage = self.secrets.set(key, secret or '')
            self.store.config_set(f'repository_secret_ref:{repository_id}', key if secret else None)
            self.store.config_set(storage_key, storage)
        if old_ref and old_ref != self.key(repository_id):
            self.store.config_update(f'repository_secret_cleanup:{repository_id}',
                                     lambda pending: [*(pending or []), {'ref': old_ref, 'storage': old_storage}], [])

    def cleanup_secrets(self, repository_id: str) -> None:
        """Called only after new connection metadata/reference are already saved."""
        key = f'repository_secret_cleanup:{repository_id}'
        remaining = []
        for item in self.store.config_get(key, []):
            try:
                if item['storage'] == 'keyring' and not self.secrets.backend:
                    raise RuntimeError('credential store unavailable')
                self.secrets.set(item['ref'], '')
                # Secrets tolerates an inaccessible backend. Here an explicit
                # removal promise requires confirming deletion, not just hiding it.
                if item['storage'] == 'keyring' and self.secrets.backend.get_password(self.secrets.service, item['ref']):
                    raise RuntimeError('credential remains')
            except Exception:
                remaining.append(item)
        self.store.config_set(key, remaining)
        if remaining:
            raise GitError('credential_cleanup', '连接设置已保存，旧凭据已停止使用，但系统凭据库未能删除旧凭据。请解锁系统凭据库后再次保存以重试清除。', 409)

    def branch(self, name: str | None) -> str:
        name = (name or '').strip()
        if name:
            if len(name) > 256 or name.startswith('-') or any(ord(c) < 32 for c in name):
                raise GitError('invalid_configuration', 'Git 分支名称无效。')
            try:
                self.run(['check-ref-format', '--branch', name])
            except CoreError:
                raise GitError('invalid_configuration', 'Git 分支名称无效。') from None
        return name

    def run(self, args: list[str], cwd: Path | None = None, timeout: int = 30,
            connection: dict | None = None, secret: str | None = None, *,
            isolated: bool = False, object_directory: str | None = None) -> str:
        if args[0] in ('clone', 'fetch', 'ls-remote') and not isolated:
            return self._network(args, cwd, timeout, connection, secret)
        # Do not inherit Git tracing, interactive prompts, or config injection. Git
        # never gets a token in argv/on disk; redirect following is always disabled.
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(('GIT_', 'SSH_ASKPASS', 'GCM_'))}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='', GCM_INTERACTIVE='never', GIT_SSH_COMMAND='false',
                   GIT_NO_LAZY_FETCH='1')
        if isolated and cwd:
            env['GIT_CEILING_DIRECTORIES'] = str(cwd.parent)
        if args[0] == 'show':
            args = ['show', '--no-ext-diff', '--no-textconv', *args[1:]]
        allow_network = 'always' if isolated and args[0] in ('clone', 'fetch', 'ls-remote') else 'never'
        config = [('core.hooksPath', os.devnull), ('core.fsmonitor', 'false'),
                  ('protocol.allow', 'never'), ('protocol.https.allow', allow_network),
                  ('protocol.http.allow', allow_network), ('protocol.ssh.allow', allow_network),
                  ('protocol.file.allow', 'never'), ('protocol.ext.allow', 'never'),
                  ('credential.helper', ''), ('http.extraHeader', ''), ('http.followRedirects', 'false'),
                  ('http.sslVerify', 'true'), ('http.proxy', ''), ('submodule.recurse', 'false'),
                  ('fetch.recurseSubmodules', 'false'), ('core.pager', 'cat'),
                  ('init.templateDir', ''), ('gc.auto', '0'), ('maintenance.auto', 'false')]
        connection = connection or {'auth_type': 'none'}
        kind = connection['auth_type']
        if kind == 'https':
            if not secret:
                raise GitError('missing_credential', '缺少 Git Token/密码。请更新凭据；仅会话凭据在退出应用后失效。', 409)
            encoded = base64.b64encode(f"{connection['username']}:{secret}".encode()).decode()
            endpoint = args[args.index('--') + 1]
            config.append((f'http.{endpoint}.extraHeader', 'Authorization: Basic ' + encoded))
        if connection.get('proxy_url'):
            config.append(('http.proxy', connection['proxy_url']))
        if connection.get('ca_bundle'):
            config.extend([('http.sslCAInfo', connection['ca_bundle']), ('http.schannelUseSSLCAInfo', 'true')])
        if kind.startswith('ssh-'):
            ssh = shutil.which('ssh')
            if not ssh:
                raise GitError('invalid_configuration', '未找到 OpenSSH 客户端，请先安装并加入 PATH。')
            hosts = connection.get('known_hosts_path') or str(Path.home() / '.ssh' / 'known_hosts')
            quoted_hosts = str(hosts).replace('\\', '/').replace('"', '\\"')
            command = [ssh.replace('\\', '/'), '-F', os.devnull, '-a', '-x', '-T',
                       '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                       '-o', 'ConnectTimeout=15',
                       '-o', f'UserKnownHostsFile="{quoted_hosts}"',
                       '-o', f'GlobalKnownHostsFile={os.devnull}', '-o', 'UpdateHostKeys=no',
                       '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
                       '-o', 'PreferredAuthentications=publickey', '-o', 'PermitLocalCommand=no',
                       '-o', 'ClearAllForwardings=yes', '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none']
            if kind == 'ssh-key':
                command += ['-o', 'IdentitiesOnly=yes', '-i', connection['ssh_key_path'].replace('\\', '/')]
            env['GIT_SSH_COMMAND'] = shlex.join(command)
            env['GIT_SSH_VARIANT'] = 'ssh'
        env['GIT_CONFIG_COUNT'] = str(len(config))
        if object_directory:
            env['GIT_OBJECT_DIRECTORY'] = object_directory
        for index, (key, value) in enumerate(config):
            env[f'GIT_CONFIG_KEY_{index}'] = key
            env[f'GIT_CONFIG_VALUE_{index}'] = value
        try:
            result = subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True,
                                    timeout=timeout, check=False)
        except FileNotFoundError:
            raise GitError('git', '未找到 Git，请先安装 Git 并加入 PATH。') from None
        except subprocess.TimeoutExpired:
            raise GitError('timeout', 'Git 操作超时，请检查公司网络/VPN、代理或仓库大小。', 408) from None
        except OSError:
            raise GitError('git', '无法启动 Git，请检查程序和目录访问权限。') from None
        if result.returncode:
            self.raise_failure(result.stderr.decode('utf-8', 'replace'))
        return result.stdout.decode('utf-8', 'replace')

    def _network(self, args: list[str], cwd: Path | None, timeout: int,
                 connection: dict | None, secret: str | None) -> str:
        """Never load a user's repo config when transmitting credentials.

        Git has no supported switch to ignore only .git/config. Use a temporary
        bare Git directory for network operations instead. Fetch writes only Git
        objects into the managed clone and returns the fetched hash directly.
        URL rewrites, remote helpers, SSL/proxy settings and hooks from the clone
        cannot affect this process; no temporary directory contains credentials.
        """
        root = self.store.data_dir / 'git-transport'
        root.mkdir(exist_ok=True)
        objects = None
        init_args = ['init', '--bare', '--template=']
        if args[0] == 'fetch':
            if cwd is None:
                raise GitError('invalid_configuration', '缺少托管仓库目录。')
            objects = str((cwd / '.git' / 'objects').resolve())
            object_format = self.run(['rev-parse', '--show-object-format'], cwd).strip()
            if object_format not in ('sha1', 'sha256'):
                raise GitError('git', '仓库对象格式不受支持。')
            init_args += ['--object-format=' + object_format]
        with tempfile.TemporaryDirectory(prefix='request-', dir=root) as temporary:
            directory = Path(temporary)
            self.run([*init_args, str(directory)], directory, isolated=True)
            output = self.run(args, directory, timeout, connection, secret,
                              isolated=True, object_directory=objects)
            if args[0] == 'fetch':
                return self.run(['rev-parse', 'FETCH_HEAD'], directory, isolated=True)
            return output

    @staticmethod
    def raise_failure(message: str) -> None:
        # Never expose untrusted Git stderr (URLs, headers, remote supplied text).
        message = message.lower()
        statuses = set(re.findall(r"(?:returned error:\s*|http(?:/[0-9.]+)?\s+(?:(?:status(?: code)?|error|code|response)\s*:?\s*)?)([1-5][0-9]{2})\b", message))
        if any(word in message for word in ('host key verification failed', 'remote host identification has changed')):
            raise GitError('ssh_host_key', 'SSH 主机密钥未知或已变化。请用可信 SSH 客户端核对公司服务器指纹并登记 known_hosts。')
        if any(word in message for word in ('certificate', 'ssl peer', 'untrusted_root', 'cert_e_', 'unknown ca')):
            raise GitError('certificate', 'TLS 证书验证失败，请配置公司 CA 证书并检查服务器证书。不会跳过证书验证。')
        if '401' in statuses or any(word in message for word in ('authentication', 'could not read username', 'publickey')):
            raise GitError('authentication', 'Git 认证失败，请检查用户名、Token/密码、SSH Agent 以及公司 SSO 授权。', 401)
        if '403' in statuses or any(word in message for word in ('permission denied', 'access denied')):
            raise GitError('permission', 'Git 拒绝访问，请确认账号具有仓库只读权限和公司授权。', 403)
        if any(word in message for word in ('resolve host', 'resolve proxy', 'could not resolve', 'failed to connect', 'connection refused', 'network is unreachable', 'connection timed out', 'ssl connect', 'tls', 'schannel')):
            raise GitError('network', '无法连接 Git 服务器，请检查公司网络/VPN、地址、端口和代理。')
        if statuses.intersection({'301', '302', '307', '308'}) or 'redirect' in message:
            raise GitError('invalid_configuration', 'Git 地址发生重定向。为保护凭据，请直接填写最终的 Git 克隆地址。')
        if 'not found' in message or '404' in statuses:
            raise GitError('permission', '仓库不存在或当前账号无权查看，请核对克隆地址和仓库权限。')
        raise GitError('git', 'Git 操作失败，请核对仓库、分支与连接设置；已有业务地图会保留。')

    def test(self, url: str, supplied: dict | None = None, repository_id: str | None = None) -> dict:
        url, remote = self.endpoint(url)
        previous = self.store.get_repository(repository_id) if repository_id else None
        if previous and previous['url'] != url:
            raise GitError('invalid_configuration', '保存的认证仅可用于原仓库地址，请为其他仓库单独填写凭据。')
        connection = self.normalize(url, supplied if supplied is not None else (previous or {}).get('connection'))
        if not remote:
            path = Path(url)
            if not path.is_dir():
                raise GitError('invalid_configuration', '本地仓库目录不存在。')
            self.run(['rev-parse', '--git-dir'], path)
            branch = self.run(['rev-parse', '--abbrev-ref', 'HEAD'], path).strip()
            return {'ok': True, 'branches': [branch] if branch != 'HEAD' else [], 'default_branch': branch if branch != 'HEAD' else None}
        secret = self.resolve_secret(connection, supplied, repository_id)
        output = self.run(['ls-remote', '--symref', '--', url, 'HEAD', 'refs/heads/*'], timeout=45,
                          connection=connection, secret=secret)
        branches, default = set(), None
        for line in output.splitlines():
            if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
                default = line[len('ref: refs/heads/'):].split('\t', 1)[0]
            elif '\trefs/heads/' in line:
                branches.add(line.split('\trefs/heads/', 1)[1])
        return {'ok': True, 'branches': sorted(branches), 'default_branch': default}

    def for_repository(self, repository: dict, args: list[str], cwd: Path | None = None, timeout: int = 90) -> str:
        connection = self.normalize(repository['url'], repository.get('connection'))
        return self.run(args, cwd, timeout, connection, self.resolve_secret(connection, None, repository['id']))
