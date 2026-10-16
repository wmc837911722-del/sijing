"""Full-analysis coverage must never silently inherit the small index sample cap."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from businessmap.core import CoreError, Store
from businessmap.source_evidence import SourceNavigator


def git(path, *args):
    return subprocess.run(['git', '-c', 'user.name=Coverage Test', '-c', 'user.email=coverage@example.invalid', *args],
                          cwd=path, check=True, capture_output=True, timeout=10).stdout.decode().strip()


@pytest.fixture
def full_repo(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    contents = {
        'src/MatchService.java': '// rule line\n' * 15_000 + 'class MatchService { int finalRule = 42; }\n',
        'src/SameRules.java': 'class SharedRule { int n = 7; }\n',
        'src/AliasRules.java': 'class SharedRule { int n = 7; }\n',
        'src/CrLf.java': 'one\r\ntwo\r\n',
        'src/Unicode.java': 'String a = "a\u2028b";\nint last = 2;',
        'src/Empty.java': '',
        'src/binary.py': 'not\x00text',
        'src/invalid.py': b'\xff\xfeinvalid',
        'src/secret.py': 'DO_NOT_SEND = "private"\n',
        'src/credentials.json': '{"value":"private"}\n',
        '.hidden/Code.java': 'private\n',
        'node_modules/demo.js': 'private\n',
        'src/generated/A.java': 'generated\n',
        'src/example.min.js': 'minified\n',
        'src/settings.py': 'API_TOKEN = "private-token-value"\nvalue = 7\n',
        'contracts/openapi.json': '{"paths":{"/seasons":{"get":{}}},"api_key":"contract-key-secret"}\n',
        'src/pages.json': '{"pages":[{"path":"pages/seasons/index"}]}\n',
        'src/routes.yaml': 'routes:\n  - path: /seasons\n',
        'pom.xml': '<project><modules><module>arena-service</module></modules></project>\n',
        'settings.gradle.kts': 'include(":feature-match")\n',
        'src/schema.sql': 'CREATE TABLE seasons (id BIGINT);\n',
        'application-pre.yaml': 'password: hidden-environment-password\n',
        'gradle.properties': 'SIGNING_PASSWORD=hidden-signing-password\n',
        'package-lock.json': '{"large":"generated dependency tree"}\n',
        'src/reference.svg': '<svg/>\n',
        'src/LongLine.java': 'String longText = "' + 'x' * 20_000 + '";\nint finalLine = 9;\n',
    }
    for name, content in contents.items():
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    (source / 'src/Shortcut.java').symlink_to('SameRules.java')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Coverage fixture')
    store = Store(tmp_path / 'data')
    store.open_vault(str(tmp_path / 'vault'))
    repo = {'id': 'coverage', 'name': 'Coverage', 'url': str(source), 'path': str(source), 'remote': False,
            'branch': '', 'commit': git(source, 'rev-parse', 'HEAD'), 'status': 'ready',
            'vault_path': str(store.vault), 'connection': {'auth_type': 'none'}}
    store._write_repo(repo)
    yield source, store, repo
    store.close()


def inventory(store, repository):
    from businessmap.full_source import FullSourceInventory
    return FullSourceInventory(store, repository)


def test_full_inventory_keeps_large_business_tail_and_pinned_snapshot(full_repo):
    source, store, repo = full_repo
    with inventory(store, repo) as inv:
        item = next(x for x in inv.files if x['path'] == 'src/MatchService.java')
        assert item['size'] > 128_000
        (source / item['path']).write_text('uncommitted poison')
        result = inv.read(item)
        assert result['content'].endswith('class MatchService { int finalRule = 42; }\n')
        assert result['total_lines'] == 15_001
        assert result['original_bytes'] == item['size']
        assert result['repository_id'] == repo['id'] and result['commit'] == repo['commit']
        assert result['redacted'] is False


def test_full_inventory_includes_contracts_configs_and_explicitly_excludes_unsafe_files(full_repo):
    _, store, repo = full_repo
    with inventory(store, repo['id']) as inv:
        files = {x['path']: x for x in inv.files}
        assert {'contracts/openapi.json', 'src/pages.json', 'src/routes.yaml', 'pom.xml', 'settings.gradle.kts', 'src/schema.sql'} <= files.keys()
        assert files['pom.xml']['kind'] == files['src/pages.json']['kind'] == 'contract'
        excluded = {x['path']: x for x in inv.excluded}
        for path in ['src/secret.py', 'src/credentials.json', '.hidden/Code.java', 'node_modules/demo.js',
                     'src/generated/A.java', 'src/example.min.js', 'application-pre.yaml', 'gradle.properties',
                     'package-lock.json', 'src/reference.svg', 'src/Shortcut.java']:
            assert path not in files
            assert excluded[path]['reason']
        assert excluded['src/Shortcut.java']['reason'] == 'non_regular'


def test_full_read_redacts_secrets_in_json_and_code_without_shifting_lines(full_repo):
    _, store, repo = full_repo
    with inventory(store, repo) as inv:
        code = inv.read('src/settings.py')
        contract = inv.read('contracts/openapi.json')
        assert code['content'] == 'API_TOKEN = "[REDACTED]"\nvalue = 7\n'
        assert code['redacted'] is contract['redacted'] is True
        assert 'contract-key-secret' not in contract['content']
        assert '"/seasons"' in contract['content']
        assert code['total_lines'] == 2 and contract['total_lines'] == 1
        assert 'original' not in code


def test_full_read_aliases_share_safe_blob_without_spawning_per_file_git(full_repo, monkeypatch):
    _, store, repo = full_repo
    original = subprocess.Popen
    started = []
    def observed(args, **kwargs):
        started.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', observed)
    with inventory(store, repo) as inv:
        a = inv.read('src/SameRules.java')
        b = inv.read('src/AliasRules.java')
        assert a['content'] == b['content'] == 'class SharedRule { int n = 7; }\n'
        assert a['blob'] == b['blob']
        assert a['path'] != b['path']
        inv.read('src/CrLf.java')
        assert len([x for x in started if 'cat-file' in x]) == 1


def test_full_read_lf_policy_keeps_crlf_unicode_and_final_unterminated_line(full_repo):
    _, store, repo = full_repo
    with inventory(store, repo) as inv:
        crlf = inv.read('src/CrLf.java')
        unicode = inv.read('src/Unicode.java')
        empty = inv.read('src/Empty.java')
        long = inv.read('src/LongLine.java')
        assert crlf['content'] == 'one\r\ntwo\r\n' and crlf['total_lines'] == 2
        assert unicode['total_lines'] == 2 and unicode['content'].endswith('int last = 2;')
        assert empty['content'] == '' and empty['total_lines'] == 0
        assert len(long['content'].split('\n')[0]) > 20_000 and long['content'].endswith('int finalLine = 9;\n')


def test_binary_and_non_utf8_are_explicit_read_failures_not_empty_success(full_repo):
    _, store, repo = full_repo
    with inventory(store, repo) as inv:
        for path in ['src/binary.py', 'src/invalid.py']:
            with pytest.raises(CoreError) as error:
                inv.read(path)
            assert error.value.status_code == 415


def test_oversize_source_is_explicitly_excluded_not_silently_truncated(full_repo, monkeypatch):
    from businessmap.full_source import FullSourceInventory
    _, store, repo = full_repo
    monkeypatch.setattr(FullSourceInventory, 'MAX_FILE_BYTES', 150_000)
    with inventory(store, repo) as inv:
        assert 'src/MatchService.java' not in {x['path'] for x in inv.files}
        excluded = next(x for x in inv.excluded if x['path'] == 'src/MatchService.java')
        assert excluded['reason'] == 'resource_limit' and excluded['size'] > 150_000
        with pytest.raises(CoreError):
            inv.read('src/MatchService.java')


def test_inventory_rejects_tampered_item_and_stale_snapshot(full_repo):
    _, store, repo = full_repo
    with inventory(store, repo) as inv:
        item = next(x for x in inv.files if x['path'] == 'src/SameRules.java')
        with pytest.raises(CoreError):
            inv.read({**item, 'blob': 'a' * 40})
        store.update_repository(repo['id'], {'commit': 'b' * 40})
        with pytest.raises(CoreError) as error:
            inv.read(item)
        assert error.value.status_code == 409


def test_source_viewer_can_read_large_tail_and_contract_with_same_redaction(full_repo):
    _, store, repo = full_repo
    nav = SourceNavigator(store, repo['id'])
    large = nav.read('src/MatchService.java', 15_001, 15_001)
    assert large['content'] == '15001: class MatchService { int finalRule = 42; }'
    assert large['reference'].endswith('/src/MatchService.java#L15001-L15001')
    contract = nav.read('contracts/openapi.json', 1, 1)
    assert 'contract-key-secret' not in contract['content']
    assert '[REDACTED]' in contract['content']


def test_multiline_secret_and_xml_password_literals_keep_lf_but_hide_values():
    from businessmap.full_source import redact_source
    text = 'password = """first-private\nsecond-private\n"""\nclient_secret = `third-private\nfourth-private`;\n<password>xml-private</password>\n'
    result, redacted = redact_source(text, 'src/settings.py')
    assert redacted and result.count('\n') == 6
    assert all(secret not in result for secret in ['first-private', 'second-private', 'third-private', 'fourth-private', 'xml-private'])


def test_same_blob_with_different_file_type_cannot_reuse_less_strict_redaction(full_repo):
    source, store, repo = full_repo
    for path in ['src/Safe.py', 'src/routes.yaml']:
        (source / path).write_text('password: private-config-value\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Cross-type aliases')
    repo['commit'] = git(source, 'rev-parse', 'HEAD')
    store._write_repo(repo)
    with inventory(store, repo) as inv:
        inv.read('src/Safe.py')
        result = inv.read('src/routes.yaml')
        assert 'private-config-value' not in result['content']
        assert result['content'] == 'password: [REDACTED]\n'


def test_closed_full_inventory_rejects_reads_and_stops_child(full_repo):
    _, store, repo = full_repo
    inv = inventory(store, repo)
    inv.read('src/SameRules.java')
    child = inv._batch
    inv.close()
    assert child.poll() is not None
    with pytest.raises(CoreError) as error:
        inv.read('src/SameRules.java')
    assert error.value.status_code == 409


def test_test_and_debug_entry_points_are_labeled_as_support_not_production_source(full_repo):
    source, store, repo = full_repo
    for path in ['src/test/java/MatchTest.java', 'src/debug/kotlin/Preview.kt', 'src/__tests__/match.test.ts',
                 'src/api/season.ts']:
        p = source / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('// fixture\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Production and support')
    repo['commit'] = git(source, 'rev-parse', 'HEAD')
    store._write_repo(repo)
    with inventory(store, repo) as inv:
        kinds = {item['path']: item['kind'] for item in inv.files}
        assert kinds['src/test/java/MatchTest.java'] == 'test'
        assert kinds['src/debug/kotlin/Preview.kt'] == 'test'
        assert kinds['src/__tests__/match.test.ts'] == 'test'
        assert kinds['src/api/season.ts'] == 'source'


def test_business_credential_and_password_classes_are_code_not_secret_files(full_repo):
    source, store, repo = full_repo
    for path, content in {
        'arena-mcp/src/main/java/com/arena/AgentCredential.java': 'class AgentCredential { String appKey = "private-app-key"; }\n',
        'arena-service/src/main/java/com/arena/ChangePasswordRequest.java': 'record ChangePasswordRequest(String password) {}\n',
        'arena-service/src/main/kotlin/com/arena/PasswordService.kt': 'class PasswordService { fun validate() = true }\n',
        'src/private-key.pem': '-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----\n',
        '.env': 'APP_KEY=private-environment\n',
    }.items():
        p = source / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Business credential types')
    repo['commit'] = git(source, 'rev-parse', 'HEAD')
    store._write_repo(repo)
    with inventory(store, repo) as inv:
        files = {x['path'] for x in inv.files}
        assert 'arena-service/src/main/java/com/arena/ChangePasswordRequest.java' in files
        assert 'arena-service/src/main/kotlin/com/arena/PasswordService.kt' in files
        result = inv.read('arena-mcp/src/main/java/com/arena/AgentCredential.java')
        assert 'private-app-key' not in result['content']
        assert 'String appKey = "[REDACTED]";' in result['content']
        assert 'src/credentials.json' not in files and 'src/secret.py' not in files
        assert '.env' not in files and 'src/private-key.pem' not in files
    viewer = SourceNavigator(store, repo['id']).read('arena-mcp/src/main/java/com/arena/AgentCredential.java')
    assert 'class AgentCredential' in viewer['content'] and 'private-app-key' not in viewer['content']


def test_long_plain_source_line_redaction_is_linear_and_still_hides_url_credentials():
    import time
    from businessmap.full_source import redact_source
    text = 'x' * 45_000 + '\nurl = "https://account:private-url-password@example.invalid/api"\n'
    started = time.monotonic()
    result, redacted = redact_source(text, 'src/Long.java')
    elapsed = time.monotonic() - started
    assert elapsed < 2, f'plain source line redaction took {elapsed:.2f}s'
    assert redacted and 'private-url-password' not in result
    assert result.startswith('x' * 45_000 + '\n')


def test_kotlin_nul_character_literal_is_text_but_other_nul_sources_remain_rejected(full_repo):
    source, store, repo = full_repo
    contents = {
        'common/src/main/kotlin/com/arena/PrettyLogger.kt': "var current = '\x00'\nval finalRule = 42\n",
        'common/src/main/java/com/arena/Logger.java': "char current = '\x00';\nint finalRule = 42;\n",
        'common/src/main/kotlin/com/arena/BadBinary.kt': 'package arena\nrandom\x00binary\n',
    }
    for path, content in contents.items():
        p = source / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode())
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Text character literal and binary distinction')
    repo['commit'] = git(source, 'rev-parse', 'HEAD')
    store._write_repo(repo)
    with inventory(store, repo) as inv:
        for path in ['common/src/main/kotlin/com/arena/PrettyLogger.kt', 'common/src/main/java/com/arena/Logger.java']:
            result = inv.read(path)
            assert result['content'] == contents[path] and result['total_lines'] == 2
        for path in ['common/src/main/kotlin/com/arena/BadBinary.kt', 'src/binary.py']:
            with pytest.raises(CoreError) as error:
                inv.read(path)
            assert error.value.status_code == 415
    viewer = SourceNavigator(store, repo['id']).read('common/src/main/kotlin/com/arena/PrettyLogger.kt', 2, 2)
    assert viewer['content'] == '2: val finalRule = 42'


def test_password_ui_tests_and_password_version_migration_are_business_source(full_repo):
    source, store, repo = full_repo
    additions = {
        'src/components/PasswordStrength.vue': '<script setup>const strength = password.length >= 8</script>\n',
        'src/views/auth/ForgetPassword.vue': '<template>重置密码</template>\n<script setup>const appKey = "private-front-app-key"</script>\n',
        'src/__tests__/common/password-strength.test.ts': 'test("password requires digits", () => checkPassword("abc123"))\n',
        'arena-service/src/main/resources/db/V1_86__alter_device_account_add_password_version.sql': 'ALTER TABLE device_account ADD password_version INT NOT NULL DEFAULT 1;\n',
        'src/components/credentials.vue': 'export default "private-credential-material"\n',
        'src/views/auth/secrets.vue': 'export default "private-secret-material"\n',
        'src/__tests__/credentials.test.ts': 'export const privateMaterial = "never-send"\n',
    }
    for path, content in additions.items():
        p = source / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Password business is not a credential data file')
    repo['commit'] = git(source, 'rev-parse', 'HEAD')
    store._write_repo(repo)
    with inventory(store, repo) as inv:
        files = {item['path']: item for item in inv.files}
        for path in list(additions)[:4]:
            assert path in files
            result = inv.read(path)
            assert result['total_lines'] > 0
        assert files['src/__tests__/common/password-strength.test.ts']['kind'] == 'test'
        assert 'private-front-app-key' not in inv.read('src/views/auth/ForgetPassword.vue')['content']
        for path in list(additions)[4:]:
            assert path not in files
        assert 'src/credentials.json' not in files and 'src/secret.py' not in files
    result = SourceNavigator(store, repo['id']).read('src/views/auth/ForgetPassword.vue', 1, 2)
    assert '重置密码' in result['content'] and 'private-front-app-key' not in result['content']


def metadata_reads(monkeypatch):
    """Observe real Git metadata scans; keep their actual output and guards."""
    import threading
    count, lock = [], threading.Lock()
    original = SourceNavigator._run_git
    def observed(self, args, **kwargs):
        if args and args[0] == 'ls-tree':
            with lock:
                count.append(tuple(args))
        return original(self, args, **kwargs)
    monkeypatch.setattr(SourceNavigator, '_run_git', observed)
    return count


def test_identical_snapshot_metadata_is_scanned_once_without_sharing_mutable_records(full_repo, monkeypatch):
    _, store, repo = full_repo
    scans = metadata_reads(monkeypatch)
    with inventory(store, repo) as first:
        original = dict(first.file_map['src/SameRules.java'])
        first.read('src/settings.py')
        first.files[0]['path'] = 'poison.java'
        first.file_map['src/SameRules.java']['blob'] = 'f' * 40
        first.excluded[0]['reason'] = 'poison'
    with inventory(store, repo) as second:
        assert len(scans) == 1
        assert second.file_map['src/SameRules.java'] == original
        assert all(item['path'] != 'poison.java' for item in second.files)
        assert all(item['reason'] != 'poison' for item in second.excluded)
        assert second.read('src/SameRules.java')['content'] == 'class SharedRule { int n = 7; }\n'


def test_metadata_singleflight_with_concurrent_readers_preserves_real_source(full_repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    _, store, repo = full_repo
    scans = metadata_reads(monkeypatch)
    start = threading.Barrier(8)
    def read(_):
        start.wait(timeout=5)
        with inventory(store, repo) as inv:
            return inv.read('src/SameRules.java')['content']
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(read, range(8)))
    assert outputs == ['class SharedRule { int n = 7; }\n'] * 8
    assert len(scans) == 1


def test_metadata_cache_invalidates_policy_resource_cap_and_reopened_vault(full_repo, monkeypatch):
    from businessmap.full_source import FullSourceInventory
    _, store, repo = full_repo
    scans = metadata_reads(monkeypatch)
    with inventory(store, repo):
        pass
    with inventory(store, repo):
        pass
    assert len(scans) == 1
    monkeypatch.setattr(FullSourceInventory, 'POLICY_VERSION', 'fixture-new-source-policy')
    with inventory(store, repo):
        pass
    assert len(scans) == 2
    monkeypatch.setattr(FullSourceInventory, 'MAX_FILE_BYTES', 150_000)
    with inventory(store, repo) as inv:
        assert 'src/MatchService.java' not in inv.file_map
    assert len(scans) == 3
    store.open_vault(str(store.vault))
    with inventory(store, repo):
        pass
    assert len(scans) == 4


def test_new_commit_cannot_use_cached_old_metadata_or_skip_read_guards(full_repo, monkeypatch):
    source, store, repo = full_repo
    scans = metadata_reads(monkeypatch)
    with inventory(store, repo) as previous:
        (source / 'src/NewRule.java').write_text('class NewRule { int n = 99; }\n')
        git(source, 'add', '.')
        git(source, 'commit', '-m', 'New immutable source version')
        current = {**repo, 'commit': git(source, 'rev-parse', 'HEAD')}
        store._write_repo(current)
        with pytest.raises(CoreError) as error:
            previous.read('src/SameRules.java')
        assert error.value.status_code == 409
        with pytest.raises(CoreError):
            inventory(store, repo)
        with inventory(store, current) as inv:
            assert inv.read('src/NewRule.java')['content'] == 'class NewRule { int n = 99; }\n'
    assert len(scans) == 2


def test_metadata_cache_evicts_snapshots_and_does_not_store_blob_text(full_repo, monkeypatch):
    import businessmap.full_source as module
    _, store, repo = full_repo
    monkeypatch.setattr(module.FullSourceInventory, 'METADATA_CACHE_ENTRIES', 1, raising=False)
    scans = metadata_reads(monkeypatch)
    other = {**repo, 'id': 'second-repository'}
    store._write_repo(other)
    with inventory(store, repo) as inv:
        inv.read('src/settings.py')
    with inventory(store, repo):
        pass
    assert len(scans) == 1
    with inventory(store, other):
        pass
    with inventory(store, repo):
        pass
    assert len(scans) == 3
    retained = repr(module._METADATA_CACHES.get(store))
    assert 'private-token-value' not in retained
    assert 'API_TOKEN =' not in retained and 'value = 7' not in retained


def test_oversize_metadata_is_not_retained_above_cache_memory_budget(full_repo, monkeypatch):
    from businessmap.full_source import FullSourceInventory
    _, store, repo = full_repo
    monkeypatch.setattr(FullSourceInventory, 'METADATA_CACHE_BYTES', 1, raising=False)
    scans = metadata_reads(monkeypatch)
    with inventory(store, repo):
        pass
    with inventory(store, repo):
        pass
    assert len(scans) == 2
