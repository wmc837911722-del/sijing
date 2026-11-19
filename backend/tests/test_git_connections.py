"""Actual private Git HTTP(S) integration; no external server or real credentials."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import shlex
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit

import pytest
import paramiko
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from businessmap.core import Store
from businessmap.git_transport import GitError


USER = 'fixture-user'
SECRET = 'fixture-only-token-not-a-real-credential'


def git(path, *args):
    return subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                           *args], cwd=path, capture_output=True, check=True, timeout=15).stdout.decode().strip()


def wait_task(store, task_id=None, success=True):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = next((t for t in store.tasks() if not task_id or t['id'] == task_id), None)
        if task and task['status'] in ('completed', 'failed'):
            assert task['status'] == ('completed' if success else 'failed'), task
            return task
        time.sleep(.02)
    pytest.fail('Git job did not finish')


@pytest.fixture
def private_git(tmp_path):
    root = tmp_path / 'http-root'
    root.mkdir()
    bare = root / 'company.git'
    git(root, 'init', '--bare', '--initial-branch=main', str(bare))
    work = tmp_path / 'working-copy'
    git(tmp_path, 'clone', str(bare), str(work))
    (work / 'order.py').write_text('total = 10\n', encoding='utf8')
    git(work, 'add', '.')
    git(work, 'commit', '-m', 'initial')
    git(work, 'push', 'origin', 'main')
    git(work, 'branch', 'release')
    git(work, 'push', 'origin', 'release')
    state = {'secret': SECRET, 'requests': [], 'redirect': False, 'forbidden': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.handle_git()

        def do_POST(self):
            self.handle_git()

        def handle_git(self):
            state['requests'].append(self.path)
            if state['redirect']:
                self.send_response(302)
                self.send_header('Location', state['redirect'])
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if state['forbidden']:
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            expected = 'Basic ' + base64.b64encode(f"{USER}:{state['secret']}".encode()).decode()
            if self.headers.get('Authorization') != expected:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Company Git Fixture"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            parsed = urlsplit(self.path)
            payload = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            env = {**os.environ, 'GIT_PROJECT_ROOT': str(root), 'GIT_HTTP_EXPORT_ALL': '1',
                   'PATH_INFO': parsed.path, 'REQUEST_METHOD': self.command, 'QUERY_STRING': parsed.query,
                   'CONTENT_TYPE': self.headers.get('Content-Type', ''), 'CONTENT_LENGTH': str(len(payload)),
                   'REMOTE_USER': USER, 'REMOTE_ADDR': '127.0.0.1'}
            if self.headers.get('Git-Protocol'):
                env['HTTP_GIT_PROTOCOL'] = self.headers['Git-Protocol']
            result = subprocess.run(['git', 'http-backend'], env=env, input=payload, capture_output=True, timeout=15)
            head, body = result.stdout.split(b'\r\n\r\n', 1)
            headers = [line.decode().split(':', 1) for line in head.split(b'\r\n') if b':' in line]
            status = next((int(value.strip().split()[0]) for key, value in headers if key.lower() == 'status'), 200)
            self.send_response(status)
            for key, value in headers:
                if key.lower() != 'status':
                    self.send_header(key, value.strip())
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'BusinessMap Git test CA')])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'company-ca.pem', tmp_path / 'server-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {**state, 'state': state, 'server': server, 'work': work, 'bare': bare,
           'url': f'https://127.0.0.1:{server.server_port}/company.git',
           'connection': {'auth_type': 'https', 'username': USER, 'secret': SECRET, 'ca_bundle': str(cert_path)}}
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'data 中文 with spaces')
    # Do not pollute the developer's OS credential store with fixture secrets.
    store.git_transport.secrets.backend = None
    yield store
    store.close()


def test_private_tls_test_clone_branch_fetch_credentials_and_no_secret_on_disk(store, private_git, monkeypatch):
    server = private_git
    transport = store.git_transport
    original_run = subprocess.run
    commands = []

    def recorded(args, **kwargs):
        if args[:1] == ['git'] and kwargs.get('env', {}).get('GIT_CONFIG_COUNT'):
            commands.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', recorded)
    result = transport.test(server['url'], server['connection'])
    assert result == {'ok': True, 'branches': ['main', 'release'], 'default_branch': 'main'}
    repo = store.add_repository(server['url'], branch='release', connection=server['connection'])
    wait_task(store)
    repo = store.get_repository(repo['id'])
    assert repo['branch'] == 'release' and repo['status'] == 'ready'
    assert repo['connection']['has_secret'] and repo['connection']['secret_storage'] == 'session'
    assert store.repository_context(repo['id'])['files'][0]['content'] == 'total = 10\n'
    clone = Path(repo['path'])
    assert not (clone / 'order.py').exists()  # Never checkout or run repository code.
    assert SECRET not in (clone / '.git' / 'config').read_text()
    # An altered origin must never receive the saved Authorization header.
    git(clone, 'remote', 'set-url', 'origin', 'https://127.0.0.1:1/attacker.git')
    git(server['work'], 'switch', 'release')
    (server['work'] / 'order.py').write_text('total = 20\n', encoding='utf8')
    git(server['work'], 'commit', '-am', 'new total')
    git(server['work'], 'push', 'origin', 'release')
    wait_task(store, store.sync_repository(repo['id'])['task_id'])
    assert store.repository_context(repo['id'])['files'][0]['content'] == 'total = 20\n'
    assert any(command[1] == 'fetch' and server['url'] in command for command in commands)
    new_secret = 'fixture-rotated-token'
    server['state']['secret'] = new_secret
    old_ref = transport.key(repo['id'])
    edited = {**server['connection'], 'secret': new_secret}
    store.update_repository_connection(repo['id'], edited, 'main')
    assert transport.key(repo['id']) != old_ref
    wait_task(store, store.sync_repository(repo['id'])['task_id'])
    assert store.get_repository(repo['id'])['branch'] == 'main'
    assert store.repository_context(repo['id'])['files'][0]['content'] == 'total = 10\n'
    saved = {**edited}
    saved.pop('secret')
    assert transport.test(server['url'], saved, repo['id'])['ok']
    forbidden = (SECRET, new_secret, base64.b64encode(f'{USER}:{SECRET}'.encode()).decode())
    serialized = json.dumps({'workspace': store.workspace(), 'tasks': store.tasks(), 'activity': store.activities(), 'argv': commands})
    for secret in forbidden:
        assert secret not in serialized
        for file in store.data_dir.glob('workspace.sqlite*'):
            assert secret.encode() not in file.read_bytes()
    with pytest.raises(GitError, match='原仓库地址'):
        transport.test(server['url'] + '/other', saved, repo['id'])
    # Clearing credentials/default branch never requires a successful connection.
    store.update_repository_connection(repo['id'], {**saved, 'secret': ''}, '')
    assert not store.get_repository(repo['id'])['connection']['has_secret']
    with pytest.raises(GitError) as error:
        transport.test(server['url'], saved, repo['id'])
    assert error.value.code == 'missing_credential'


def test_tls_auth_permission_redirect_errors_preserve_existing_map(store, private_git):
    url, connection = private_git['url'], private_git['connection']
    with pytest.raises(GitError) as error:
        store.git_transport.test(url, {**connection, 'ca_bundle': ''})
    assert error.value.code == 'certificate'
    with pytest.raises(GitError) as error:
        store.git_transport.test(url, {**connection, 'secret': 'wrong-token'})
    assert error.value.code == 'authentication'
    repo = store.add_repository(url, connection=connection)
    wait_task(store)
    before = {note['path']: store.read_note(note['path'])['content'] for note in store.notes()}
    private_git['state']['forbidden'] = True
    task = wait_task(store, store.sync_repository(repo['id'])['task_id'], success=False)
    assert store.get_repository(repo['id'])['last_error']['code'] == 'permission'
    assert SECRET not in json.dumps(task)
    assert before == {note['path']: store.read_note(note['path'])['content'] for note in store.notes()}
    private_git['state']['forbidden'] = False
    private_git['state']['redirect'] = 'https://127.0.0.1:1/cross-origin.git'
    with pytest.raises(GitError) as error:
        store.git_transport.test(url, connection)
    assert error.value.code == 'invalid_configuration'
    # Closing the actual endpoint exercises network errors without outside traffic.
    private_git['server'].shutdown()
    private_git['server'].server_close()
    with pytest.raises(GitError) as error:
        store.git_transport.test(url, connection)
    assert error.value.code == 'network'


def test_reject_unsafe_endpoints_options_and_redact_remote_error(store, tmp_path, monkeypatch):
    transport = store.git_transport
    for url in ('https://user:secret@git.invalid/repo.git', 'https://git.invalid/repo?token=x',
                'ext::evil', 'file:///repo', 'http://company.invalid/repo', 'ssh://-evil@git.invalid/repo'):
        with pytest.raises(GitError):
            normalized, remote = transport.endpoint(url)
            if not remote:
                transport.test(normalized)
    for branch in ('-upload-pack=evil', 'main\n--evil', '../branch', 'refs/heads/bad space'):
        with pytest.raises(GitError):
            transport.branch(branch)
    for proxy in ('http://user:pass@proxy.invalid', 'http://proxy.invalid/path', 'http://proxy.invalid\nX: y'):
        with pytest.raises(GitError):
            transport.normalize('https://git.invalid/repo', {'proxy_url': proxy})
    with pytest.raises(GitError) as error:
        transport.raise_failure('remote: Authorization Basic abcdef secret-token https://user:pass@host failed')
    assert all(value not in str(error.value) for value in ('abcdef', 'secret-token', 'user:pass'))
    marker = tmp_path / 'must-not-execute'
    key = tmp_path / "ssh key ' $(touch injected).pem"
    hosts = tmp_path / 'known hosts'
    key.touch()
    hosts.touch()
    captured = []

    def fake_run(args, **kwargs):
        captured.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'', b'')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setenv('GIT_TRACE', '1')
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.fsmonitor')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'malicious-script')
    connection = transport.normalize('ssh://git@company.invalid/repo.git',
                                     {'auth_type': 'ssh-key', 'ssh_key_path': str(key), 'known_hosts_path': str(hosts)})
    transport.run(['ls-remote', '--', 'ssh://git@company.invalid/repo.git'], connection=connection)
    args, kwargs = captured[-1]
    env = kwargs['env']
    ssh = shlex.split(env['GIT_SSH_COMMAND'])
    assert ssh[ssh.index('-i') + 1] == str(key).replace('\\', '/')
    assert 'StrictHostKeyChecking=yes' in ssh and 'ProxyCommand=none' in ssh and 'PermitLocalCommand=no' in ssh
    assert ssh[ssh.index('-F') + 1] == os.devnull
    assert 'GIT_TRACE' not in env and 'malicious-script' not in json.dumps(env)
    config = {env[f'GIT_CONFIG_KEY_{i}']: env[f'GIT_CONFIG_VALUE_{i}'] for i in range(int(env['GIT_CONFIG_COUNT']))}
    assert config['protocol.ext.allow'] == config['protocol.file.allow'] == 'never'
    assert config['credential.helper'] == '' and config['core.fsmonitor'] == 'false'
    assert not marker.exists()


def test_connection_api_does_not_echo_invalid_secrets(tmp_path):
    from fastapi.testclient import TestClient
    from businessmap.app import create_app
    app = create_app(tmp_path / 'api-data', 'fixture-session-token-long-enough')
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer fixture-session-token-long-enough'
        response = client.post('/api/repositories/test', json={
            'url': 'https://company.invalid/repo.git',
            'connection': {'auth_type': 'invalid', 'secret': SECRET}})
        assert response.status_code == 422
        assert SECRET not in response.text


def test_network_does_not_read_malicious_clone_or_parent_config(store, private_git, monkeypatch):
    captured = []

    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            captured.append(dict(self.headers))
            self.send_response(500)
            self.end_headers()

    attacker = ThreadingHTTPServer(('127.0.0.1', 0), Capture)
    thread = threading.Thread(target=attacker.serve_forever, daemon=True)
    thread.start()
    try:
        url, connection = private_git['url'], private_git['connection']
        repo = store.add_repository(url, connection=connection)
        wait_task(store)
        clone = Path(repo['path'])
        attacker_url = f'http://127.0.0.1:{attacker.server_port}/'
        git(clone, 'config', f'url.{attacker_url}.insteadOf', url)
        git(clone, 'config', f'http.{url}.sslVerify', 'false')
        git(clone, 'config', f'http.{url}.proxy', attacker_url)
        git(clone, 'config', 'credential.helper', '!echo must-not-run')
        # Network operations must neither inherit the caller repo nor use the
        # managed clone's settings. Moving cwd reproduces the parent-config case.
        monkeypatch.chdir(clone)
        assert store.git_transport.test(url, connection)['ok']
        second = store.add_repository(url, connection=connection)
        wait_task(store)
        assert store.get_repository(second['id'])['status'] == 'ready'
        wait_task(store, store.sync_repository(repo['id'])['task_id'])
        with pytest.raises(GitError) as error:
            store.git_transport.for_repository({**repo, 'connection': {**connection, 'ca_bundle': ''}},
                                               ['fetch', '--depth=1', '--', url, 'refs/heads/main'], clone)
        assert error.value.code == 'certificate'
        assert captured == []
        assert list((store.data_dir / 'git-transport').iterdir()) == []
        # A poll in progress must finish before settings can be replaced.
        store._polling_repos.add(repo['id'])
        with pytest.raises(Exception, match='正在处理'):
            store.update_repository_connection(repo['id'], connection)
        store._polling_repos.discard(repo['id'])
    finally:
        attacker.shutdown()
        attacker.server_close()
        thread.join(timeout=3)


@pytest.fixture
def ssh_git(tmp_path, private_git):
    host_key, client_key = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "client key ' $(mkdir BM_INJECTION_PROBE).pem"
    client_key.write_private_key_file(str(key_path))
    if os.name == 'nt':
        subprocess.run(['icacls', str(key_path), '/inheritance:r', '/grant:r',
                        f"{os.environ['USERNAME']}:F"], check=True, capture_output=True)
    else:
        key_path.chmod(0o600)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(.2)
    port = listener.getsockname()[1]
    hosts = tmp_path / "trusted hosts ' spaced"
    hosts.write_text(f'[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n', encoding='utf8')
    stopped = threading.Event()
    transports, workers = [], []
    commands = []

    class Server(paramiko.ServerInterface):
        def __init__(self):
            self.requested = threading.Event()

        def check_auth_publickey(self, username, key):
            return paramiko.AUTH_SUCCESSFUL if username == 'git' and key == client_key else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return 'publickey'

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            if command not in (b"git-upload-pack '/company.git'", b"git-upload-pack 'company.git'"):
                return False
            commands.append(command)
            self.requested.set()
            return True

    def session(sock):
        transport = paramiko.Transport(sock)
        transports.append(transport)
        transport.add_server_key(host_key)
        server = Server()
        process = None
        try:
            transport.start_server(server=server)
            channel = transport.accept(10)
            if channel is None or not server.requested.wait(10):
                return
            # The fixture serves one known bare repository, never client commands.
            process = subprocess.Popen(['git', 'upload-pack', str(private_git['bare'])],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            def inbound():
                try:
                    while data := channel.recv(65536):
                        process.stdin.write(data)
                        process.stdin.flush()
                except (OSError, EOFError, BrokenPipeError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass

            input_thread = threading.Thread(target=inbound, daemon=True)
            input_thread.start()
            while data := os.read(process.stdout.fileno(), 65536):
                channel.sendall(data)
            status = process.wait(timeout=10)
            channel.send_exit_status(status)
            channel.shutdown_write()
            input_thread.join(timeout=2)
            channel.close()
        except (paramiko.SSHException, EOFError, OSError):
            pass  # Unknown host-key clients intentionally disconnect early.
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            # After exit-status/EOF, let the SSH client finish its disconnect.
            # An immediate TCP close can race OpenSSH and report a broken pipe.
            transport.join(timeout=3)
            transport.close()

    def serve():
        while not stopped.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=session, args=(sock,), daemon=True)
            workers.append(worker)
            worker.start()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {'url': f'ssh://git@127.0.0.1:{port}/company.git',
           'port': port, 'commands': commands,
           'connection': {'auth_type': 'ssh-key', 'ssh_key_path': str(key_path), 'known_hosts_path': str(hosts)}}
    stopped.set()
    listener.close()
    for transport in transports:
        transport.close()
    for worker in workers:
        worker.join(timeout=3)
    thread.join(timeout=3)


def test_ssh_fixture_waits_for_peer_disconnect_after_normal_eof(ssh_git, monkeypatch):
    """Closing TCP before the client disconnects can make OpenSSH report EPIPE."""
    server_closed = threading.Event()
    original_close = paramiko.Transport.close
    def observed_close(transport):
        original_close(transport)
        if transport.server_mode:
            server_closed.set()
    monkeypatch.setattr(paramiko.Transport, 'close', observed_close)
    client = paramiko.SSHClient()
    client.load_host_keys(ssh_git['connection']['known_hosts_path'])
    try:
        client.connect('127.0.0.1', port=ssh_git['port'], username='git',
                       key_filename=ssh_git['connection']['ssh_key_path'],
                       allow_agent=False, look_for_keys=False, timeout=3)
        transport = client.get_transport()
        channel = transport.open_session(timeout=3)
        channel.exec_command("git-upload-pack '/company.git'")
        channel.sendall(b'0000')
        channel.shutdown_write()
        advertised = b''
        while data := channel.recv(65536):
            advertised += data
        assert b'refs/heads/main' in advertised
        assert channel.recv_exit_status() == 0
        # The client deliberately has not sent its disconnect yet. Observe
        # actual server teardown, rather than relying on a random clone race.
        assert not server_closed.wait(.5)
        assert transport.is_active()
    finally:
        client.close()
    assert server_closed.wait(3)


def test_real_ssh_key_spaces_quotes_and_strict_known_hosts(store, ssh_git, tmp_path, monkeypatch):
    url, connection = ssh_git['url'], ssh_git['connection']
    assert store.git_transport.test(url, connection)['branches'] == ['main', 'release']
    repo = store.add_repository(url, branch='release', connection=connection)
    wait_task(store)
    assert store.repository_context(repo['id'])['files'][0]['content'] == 'total = 10\n'
    wait_task(store, store.sync_repository(repo['id'])['task_id'])
    unknown_hosts = tmp_path / 'empty known hosts'
    unknown_hosts.touch()
    with pytest.raises(GitError) as error:
        store.git_transport.test(url, {**connection, 'known_hosts_path': str(unknown_hosts)})
    assert error.value.code == 'ssh_host_key'
    assert unknown_hosts.read_text() == ''  # No automatic trust registration.
    assert not list(tmp_path.rglob('BM_INJECTION_PROBE'))
    # SCP has no port syntax. Route its real SSH client to the fixture's random
    # port without changing the URL or the command Git sends to the SSH server.
    original_run = subprocess.run
    scp_url = 'git@127.0.0.1:company.git'

    def fixture_port(args, **kwargs):
        if scp_url in args and kwargs.get('env', {}).get('GIT_SSH_COMMAND'):
            kwargs['env']['GIT_SSH_COMMAND'] += f" -p {ssh_git['port']}"
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', fixture_port)
    assert store.git_transport.endpoint(scp_url) == (scp_url, True)
    assert store.git_transport.endpoint('git@company.invalid:/srv/repo.git') == ('git@company.invalid:/srv/repo.git', True)
    scp_repo = store.add_repository(scp_url, branch='release', connection=connection)
    wait_task(store)
    assert scp_repo['name'] == 'company' and scp_repo['url'] == scp_url
    assert b"git-upload-pack 'company.git'" in ssh_git['commands']
    wait_task(store, store.sync_repository(scp_repo['id'])['task_id'])


def test_secret_rotation_cleanup_failure_and_session_fallback(store, private_git):
    class CredentialBackend:
        def __init__(self):
            self.values = {}
            self.fail_set = self.fail_delete = False

        def set_password(self, service, key, value):
            if self.fail_set:
                raise RuntimeError('locked')
            self.values[key] = value

        def get_password(self, service, key):
            return self.values.get(key)

        def delete_password(self, service, key):
            if self.fail_delete:
                raise RuntimeError('locked')
            self.values.pop(key, None)

    transport = store.git_transport
    backend = CredentialBackend()
    transport.secrets.backend = backend
    connection = private_git['connection']
    repo = store.add_repository(private_git['url'], connection=connection)
    wait_task(store)
    first = transport.key(repo['id'])
    assert first in backend.values
    store.update_repository_connection(repo['id'], {**connection, 'secret': 'rotated-fixture'})
    second = transport.key(repo['id'])
    assert first not in backend.values and backend.values[second] == 'rotated-fixture'
    backend.fail_set = True
    store.update_repository_connection(repo['id'], {**connection, 'secret': 'session-fixture'})
    session_ref = transport.key(repo['id'])
    assert second not in backend.values
    assert store.get_repository(repo['id'])['connection']['secret_storage'] == 'session'
    # Simulate process exit: a failed OS save must never reactivate an old key.
    from businessmap.providers import _SECRET_MEMORY
    _SECRET_MEMORY.pop((transport.secrets.service, session_ref))
    assert transport.resolve_secret(connection, None, repo['id']) is None
    backend.fail_set = False
    store.update_repository_connection(repo['id'], connection)
    saved_ref = transport.key(repo['id'])
    backend.fail_delete = True
    anonymous = {'auth_type': 'none', 'ca_bundle': connection['ca_bundle']}
    with pytest.raises(GitError) as error:
        store.update_repository_connection(repo['id'], anonymous)
    assert error.value.code == 'credential_cleanup'
    saved = store.get_repository(repo['id'])
    assert saved['connection']['auth_type'] == 'none' and not saved['connection']['has_secret']
    assert transport.key(repo['id']) is None and saved_ref in backend.values
    assert store.config_get(f"repository_secret_cleanup:{repo['id']}")
    backend.fail_delete = False
    store.update_repository_connection(repo['id'], anonymous)
    assert saved_ref not in backend.values
    assert store.config_get(f"repository_secret_cleanup:{repo['id']}") == []


@pytest.mark.parametrize('message,expected', [
    ("fatal: unable to access 'https://127.0.0.1:40301/company.git': The requested URL returned error: 302", 'invalid_configuration'),
    ("fatal: unable to access 'https://127.0.0.1:40123/company.git': The requested URL returned error: 403", 'permission'),
    ("fatal: request-id=40301999; The requested URL returned error: 401", 'authentication'),
    ("HTTP/2 403 Forbidden; request-id=40123999", 'permission'),
])
def test_http_status_classification_ignores_port_and_request_number_substrings(store, message, expected):
    with pytest.raises(GitError) as error:
        store.git_transport.raise_failure(message + ' private-test-token')
    assert error.value.code == expected
    assert 'private-test-token' not in str(error.value)
